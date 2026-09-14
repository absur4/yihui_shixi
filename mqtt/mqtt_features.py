"""Native features are functional evidence, never folded into comparable rankings."""
import json
import queue
import socket
import threading
import time
import uuid
from pathlib import Path

import jsonschema
import paho.mqtt.client as mqtt

from mqtt_wire import pack, unpack


def run_features(output):
    from mqtt_adapter import Broker, ROOT, normalize, utc, write_json
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    broker = Broker(normalize({}), output)
    clients, evidence, profiles = [], [], []
    prefix = 'native/'+uuid.uuid4().hex

    def client(name, subscription=None, qos=1, clean=True, will=None):
        connected, subscribed, messages = threading.Event(), threading.Event(), queue.Queue()
        c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=prefix.replace('/', '-')+'-'+name,
                        clean_session=clean, protocol=mqtt.MQTTv311)
        c.session_present = None

        def connect(c, u, flags, reason, properties):
            if reason.is_failure:
                return
            c.session_present = flags.session_present
            connected.set()
            if subscription:
                c.subscribe(subscription, qos=qos)

        c.on_connect = connect
        c.on_subscribe = lambda c,u,m,r,p: subscribed.set() if not any(x.is_failure for x in r) else None
        c.on_message = lambda c,u,m: messages.put(dict(topic=m.topic, payload=m.payload, qos=m.qos, retain=m.retain))
        if will:
            c.will_set(will[0], will[1], qos=1)
        c.connect('127.0.0.1', broker.port, 5)
        c.loop_start()
        clients.append(c)
        if not connected.wait(5) or (subscription and not subscribed.wait(5)):
            raise TimeoutError('Feature endpoint not ready')
        return c, messages

    def publish(c, topic, payload, qos=1, retain=False):
        info = c.publish(topic, payload, qos=qos, retain=retain)
        info.wait_for_publish(5)
        assert info.is_published(), 'Publish did not complete'

    def check(name, method, fn, category='QoS'):
        record = dict(feature_name=name, category=category, status='not_tested', test_method=method,
                      configuration={'protocol':'MQTT 3.1.1','broker_version':broker.version},
                      evidence=[], performance_impact=None,
                      limitations=['Functional verification only; no comparable performance impact measured'])
        try:
            detail = fn()
            record['status'] = 'supported'
            record['evidence'] = [dict(timestamp=utc(), result=detail)]
        except Exception as exc:
            record['status'] = 'partial'
            record['evidence'] = [dict(timestamp=utc(), error=repr(exc))]
        profiles.append(record)
        print(f'FEATURE {name}: {record["status"]}', flush=True)

    try:
        broker.start()
        pub, _ = client('publisher')

        def qos_check():
            rows=[]
            for qos in (0,1,2):
                topic=prefix+f'/qos{qos}'
                sub, messages=client(f'sub-{qos}',topic,qos=qos)
                publish(pub,topic,b'qos-evidence',qos=qos)
                m=messages.get(timeout=5)
                assert m['payload']==b'qos-evidence' and m['qos']==qos
                rows.append(dict(qos=qos,received_qos=m['qos'],publication_completed=True))
            return rows
        check('QoS 0/1/2','Publish at each QoS; verify received QoS and successful protocol completion',qos_check)

        def retained():
            topic=prefix+'/retained'
            publish(pub,topic,b'retained-evidence',retain=True)
            sub,messages=client('retained-subscriber',topic)
            m=messages.get(timeout=5)
            assert m['payload']==b'retained-evidence' and m['retain']
            publish(pub,topic,b'',retain=True)
            return dict(delivered_to_new_subscriber=True,retain_flag=m['retain'],retained_value_cleared=True)
        check('Retained Message','Publish retained value before subscriber connects, verify retained replay, then clear it',retained)

        def will():
            topic=prefix+'/will'
            sub,messages=client('will-observer',topic)
            doomed,_=client('doomed',will=(topic,b'unexpected-disconnect'))
            doomed.loop_stop()
            sock=doomed.socket()
            sock.shutdown(socket.SHUT_RDWR)
            sock.close()  # no MQTT DISCONNECT sent
            m=messages.get(timeout=7)
            assert m['payload']==b'unexpected-disconnect'
            return dict(ungraceful_transport_close=True,will_received=True,qos=m['qos'])
        check('Last Will','Abort connected client socket without MQTT DISCONNECT; observe broker-published will',will,'reliability')

        def persistent():
            topic=prefix+'/persistent'
            sub,_=client('persistent-subscriber',topic,clean=False)
            sub.disconnect(); sub.loop_stop()
            for i in range(10):
                publish(pub,topic,str(i).encode(),qos=1)
            resumed,messages=client('persistent-subscriber',clean=False)
            values=[int(messages.get(timeout=5)['payload']) for _ in range(10)]
            assert resumed.session_present and sorted(values)==list(range(10))
            return dict(session_present=True,offline_messages=10,received_unique=len(set(values)),values=values)
        check('Persistent Session','Subscribe with clean_session=false, disconnect, publish QoS1 offline, reconnect same client ID without resubscribe',persistent,'reliability')

        def integrity():
            topic=prefix+'/integrity'
            sub,messages=client('integrity-subscriber',topic)
            run=uuid.uuid4().bytes
            valid=pack(run,0,0,time.perf_counter_ns(),b'x'*32)
            mutated=bytearray(valid);mutated[-1]^=1
            cases=[('valid',valid),('checksum_error',bytes(mutated)),('length_error',valid[:-1]),
                   ('unparseable',b'bad'),('foreign_run',pack(uuid.uuid4().bytes,0,0,time.perf_counter_ns(),b'x'*32))]
            observed=[]
            for expected,data in cases:
                publish(pub,topic,data)
                received=messages.get(timeout=5)['payload']
                _,status=unpack(received,run,32,1)
                assert status==expected,(status,expected)
                observed.append(dict(expected=expected,observed=status))
            return observed
        check('Payload integrity diagnostics','Send deliberately valid, corrupted, truncated, unparseable and foreign-run envelopes through the real broker',integrity,'correctness')
    finally:
        for c in clients:
            try:
                c.disconnect(); c.loop_stop()
            except OSError:
                pass
        broker.stop()
    validator=jsonschema.Draft202012Validator(json.loads((ROOT/'feature_profile.schema.json').read_text(encoding='utf-8')))
    for profile in profiles:
        validator.validate(profile)
    write_json(output/'feature_profile.json',dict(schema_version='1.0',test_time=utc(),features=profiles))
    lines=['# MQTT 原生功能特性矩阵','','| 特性 | 状态 | 方法 |','|---|---|---|']
    lines += [f"| {p['feature_name']} | {p['status']} | {p['test_method']} |" for p in profiles]
    (output/'feature_matrix.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    if any(p['status']!='supported' for p in profiles):
        raise RuntimeError('At least one feature test failed; see feature_profile.json')

"""Fast DDS 传输映射与参与者 XML profile。

对外 `transport_mode` 统一使用小写 `udp` / `shm`（DDS_readme_1.md §3.4、§5.5），
内部再映射到 Fast DDS 的显式 transport descriptor，并把映射写入结果
`transport_detail`，避免含糊。
"""

from __future__ import annotations

from pathlib import Path
from xml.etree import ElementTree as ET

PROFILE_NAME = "benchmark_participant"

# 对外小写值 -> Fast DDS 实际传输
TRANSPORT_MODES = {
    "udp": "UDPv4",
    "shm": "SHM",
    "default": "DEFAULT",
}

# 必须随结果一起写入的传输细节（DDS_readme_1.md §5.5）
TRANSPORT_DETAILS = {
    "udp": "UDPv4, builtin transports disabled, data sharing off",
    "shm": "SHM descriptor, builtin transports disabled, data sharing off",
    "default": "builtin transports (UDPv4 + SHM), data sharing off",
}

_UDP_DESCRIPTOR = """
      <transport_descriptors>
        <transport_descriptor>
          <transport_id>benchmark_udpv4</transport_id>
          <type>UDPv4</type>
          <sendBufferSize>{buffer}</sendBufferSize>
          <receiveBufferSize>{buffer}</receiveBufferSize>
          <non_blocking_send>false</non_blocking_send>
        </transport_descriptor>
      </transport_descriptors>"""

_SHM_DESCRIPTOR = """
      <transport_descriptors>
        <transport_descriptor>
          <transport_id>benchmark_shm</transport_id>
          <type>SHM</type>
        </transport_descriptor>
      </transport_descriptors>"""

_EXPLICIT_TRANSPORT = """
          <userTransports>
            <transport_id>{transport_id}</transport_id>
          </userTransports>
          <useBuiltinTransports>false</useBuiltinTransports>"""


def transport_detail(transport_mode: str) -> str:
    """返回该对外传输模式必须写入结果的说明文本。"""
    try:
        return TRANSPORT_DETAILS[str(transport_mode).lower()]
    except KeyError:
        raise ValueError(f"Unsupported transport mode: {transport_mode}") from None


def render_transport_xml(
    path: Path,
    transport_mode: str,
    participant_name: str,
    socket_buffer_size: int = 4 * 1024 * 1024,
) -> Path:
    """生成 Fast DDS XML profile，避免 UDP 条件意外回落到共享内存。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = str(transport_mode).lower()

    if mode == "udp":
        descriptor = _UDP_DESCRIPTOR.format(buffer=socket_buffer_size)
        rtps_transport = _EXPLICIT_TRANSPORT.format(transport_id="benchmark_udpv4")
    elif mode == "shm":
        descriptor = _SHM_DESCRIPTOR
        rtps_transport = _EXPLICIT_TRANSPORT.format(transport_id="benchmark_shm")
    elif mode == "default":
        descriptor = ""
        rtps_transport = "<useBuiltinTransports>true</useBuiltinTransports>"
    else:
        raise ValueError(f"Unsupported transport mode: {transport_mode}")

    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<dds xmlns="http://www.eprosima.com">
  <profiles>{descriptor}
    <participant profile_name="{PROFILE_NAME}">
      <rtps>
        <name>{participant_name}</name>
        {rtps_transport}
      </rtps>
    </participant>
  </profiles>
</dds>
"""
    # 先解析再写盘，保证畸形 XML 永远不会交给 Fast DDS。
    ET.fromstring(xml)
    path.write_text(xml, encoding="utf-8", newline="\n")
    return path

from __future__ import annotations

from pathlib import Path
from xml.etree import ElementTree as ET

PROFILE_NAME = "benchmark_participant"


def render_transport_xml(
    path: Path,
    transport_mode: str,
    participant_name: str,
    socket_buffer_size: int = 4 * 1024 * 1024,
) -> Path:
    """Create a Fast DDS XML profile that prevents accidental SHM use in UDP tests."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    if transport_mode == "UDPv4":
        descriptor = f"""
      <transport_descriptors>
        <transport_descriptor>
          <transport_id>benchmark_udpv4</transport_id>
          <type>UDPv4</type>
          <sendBufferSize>{socket_buffer_size}</sendBufferSize>
          <receiveBufferSize>{socket_buffer_size}</receiveBufferSize>
          <non_blocking_send>false</non_blocking_send>
        </transport_descriptor>
      </transport_descriptors>"""
        rtps_transport = """
          <userTransports>
            <transport_id>benchmark_udpv4</transport_id>
          </userTransports>
          <useBuiltinTransports>false</useBuiltinTransports>"""
    elif transport_mode == "SHM":
        descriptor = """
      <transport_descriptors>
        <transport_descriptor>
          <transport_id>benchmark_shm</transport_id>
          <type>SHM</type>
        </transport_descriptor>
      </transport_descriptors>"""
        rtps_transport = """
          <userTransports>
            <transport_id>benchmark_shm</transport_id>
          </userTransports>
          <useBuiltinTransports>false</useBuiltinTransports>"""
    elif transport_mode == "DEFAULT":
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
    # Parse before writing so malformed generated XML never reaches Fast DDS.
    ET.fromstring(xml)
    path.write_text(xml, encoding="utf-8", newline="\n")
    return path


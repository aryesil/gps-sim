"""Xilinx .bit -> .bin for the Zynq PCAP (xdevcfg): payload after the header, every 32-bit word
byte-swapped, padded with NOP words (0x20000000) to a multiple of 64 bytes, as bootgen does
(checked against the r6 and gpsdod3 images).

usage: python scripts/bit2bin.py in.bit out.bin
"""
import struct
import sys


def payload(bit: bytes) -> bytes:
    n = struct.unpack(">H", bit[:2])[0]
    i = 2 + n + 2                       # 9-byte magic, then the 0x0001 marker
    for tag in b"abcd":                 # design, part, date, time: 2-byte length strings
        assert bit[i] == tag, f"field {chr(tag)} expected at {i}"
        i += 1
        i += 2 + struct.unpack(">H", bit[i:i + 2])[0]
    assert bit[i] == ord("e")
    ln = struct.unpack(">I", bit[i + 1:i + 5])[0]
    return bit[i + 5:i + 5 + ln]


if __name__ == "__main__":
    data = bytearray(payload(open(sys.argv[1], "rb").read()))
    assert len(data) % 4 == 0
    for i in range(0, len(data), 4):
        data[i:i + 4] = data[i:i + 4][::-1]
    data += bytes.fromhex("00000020") * (-len(data) % 64 // 4)
    open(sys.argv[2], "wb").write(bytes(data))
    print(len(data), "bytes")

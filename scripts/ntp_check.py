import ntplib, sys

def offset_ms(server: str) -> float:
    r = ntplib.NTPClient().request(server, version=3, timeout=5)
    return r.offset * 1000

if __name__ == "__main__":
    server = sys.argv[1] if len(sys.argv) > 1 else "pool.ntp.org"
    off = offset_ms(server)
    print(f"NTP offset vs {server}: {off:.2f} ms")
    sys.exit(0 if abs(off) < 50 else 1)
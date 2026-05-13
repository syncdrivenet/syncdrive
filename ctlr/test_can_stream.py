#!/usr/bin/env python3
"""
Test script to watch raw CAN data from ESP32.

Usage:
    sudo systemctl stop syncdrive-ctlr
    python3 test_can_stream.py
    # Ctrl+C to stop
    sudo systemctl start syncdrive-ctlr
"""

import asyncio
import signal
import sys
from datetime import datetime

PORT = 9101
running = True


def signal_handler(sig, frame):
    global running
    print("\n\nStopping...")
    running = False
    sys.exit(0)


async def handle_client(reader, writer):
    addr = writer.get_extra_info('peername')
    print(f"\n{'='*60}")
    print(f"ESP32 CONNECTED from {addr[0]}:{addr[1]}")
    print(f"{'='*60}")
    print(f"Waiting for CAN data...\n")
    print(f"{'TIMESTAMP':<20} {'CAN ID':<10} {'LEN':<5} {'DATA':<20}")
    print(f"{'-'*20} {'-'*10} {'-'*5} {'-'*20}")

    frame_count = 0

    try:
        while running:
            line = await reader.readline()
            if not line:
                break

            line_str = line.decode('utf-8').strip()
            if not line_str or line_str.startswith('ts,'):
                continue

            frame_count += 1

            # Parse: ts,id,len,data
            try:
                parts = line_str.split(',')
                if len(parts) >= 4:
                    raw_ts = float(parts[0])
                    # Convert ms to seconds if needed
                    if raw_ts > 1e12:
                        ts = raw_ts / 1000.0
                    else:
                        ts = raw_ts

                    ts_str = datetime.fromtimestamp(ts).strftime('%H:%M:%S.%f')[:-3]
                    can_id = parts[1]
                    length = parts[2]
                    data = parts[3]

                    print(f"{ts_str:<20} {can_id:<10} {length:<5} {data:<20}")
                else:
                    print(f"[RAW] {line_str}")

            except Exception as e:
                print(f"[PARSE ERROR] {line_str} - {e}")

    except asyncio.CancelledError:
        pass
    except Exception as e:
        print(f"[ERROR] {e}")
    finally:
        print(f"\n{'='*60}")
        print(f"ESP32 DISCONNECTED - {frame_count} frames received")
        print(f"{'='*60}\n")
        writer.close()
        await writer.wait_closed()


async def main():
    signal.signal(signal.SIGINT, signal_handler)

    print(f"""
╔══════════════════════════════════════════════════════════════╗
║              CAN BUS TEST MONITOR                            ║
╠══════════════════════════════════════════════════════════════╣
║  Listening on port {PORT}                                      ║
║  Press Ctrl+C to stop                                        ║
╚══════════════════════════════════════════════════════════════╝
""")

    server = await asyncio.start_server(handle_client, '0.0.0.0', PORT)
    print(f"Waiting for ESP32 to connect...\n")

    async with server:
        await server.serve_forever()


if __name__ == '__main__':
    asyncio.run(main())

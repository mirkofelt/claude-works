"""Tor daemon management."""
import asyncio
import os


async def restart_tor() -> str:
    """Start or restart Tor daemon inside container. Returns status string."""
    try:
        os.makedirs("/var/lib/tor", exist_ok=True)
        os.makedirs("/run/tor", exist_ok=True)
        proc = await asyncio.create_subprocess_exec(
            "tor", "--RunAsDaemon", "1",
            "--DataDirectory", "/var/lib/tor",
            "--PidFile", "/run/tor/tor.pid",
            "--Log", "warn stderr",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=10.0)
        if proc.returncode != 0:
            return f"tor start failed (exit {proc.returncode})"
        for _ in range(60):
            try:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection("127.0.0.1", 9050), timeout=1.0
                )
                writer.close()
                await writer.wait_closed()
                return "Tor started — SOCKS5 ready on 127.0.0.1:9050"
            except Exception:
                await asyncio.sleep(1.0)
        return "Tor process started but port 9050 not ready after 60s"
    except asyncio.TimeoutError:
        return "tor start timed out (10s)"
    except Exception as e:
        return f"tor restart error: {type(e).__name__}"

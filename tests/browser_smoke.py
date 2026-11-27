"""Real browser regression test. Run separately from the hardware-free unit suite.

Requires the web extra, Playwright and its Chromium browser.
"""
import base64
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def main():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    credential = base64.b64encode(b"dsc:browser-test-password").decode()
    with tempfile.TemporaryFile(mode="w+") as log:
        process = subprocess.Popen([sys.executable, str(Path(__file__).with_name("web_fixture.py")),
                                    "--port", str(port)], stdout=log, stderr=log)
        try:
            for _ in range(100):
                try:
                    urlopen(url, timeout=1)
                except HTTPError as exc:
                    assert exc.code == 401
                    break
                except (URLError, OSError):
                    if process.poll() is not None:
                        raise RuntimeError("Test server exited")
                    time.sleep(0.1)
            else:
                raise RuntimeError("Test server did not start")
            request = Request(url, headers={"Authorization": "Basic " + credential})
            with urlopen(request, timeout=10) as response:
                assert response.status == 200
                assert "DSC station" in response.read().decode()
            if "--http-only" in sys.argv:
                print("Authenticated page rendered; unauthenticated HTTP rejected.")
                return
            from playwright.sync_api import sync_playwright, expect
            with sync_playwright() as p:
                browser = p.chromium.launch(executable_path=os.environ.get("DSC_BROWSER_EXECUTABLE"))
                errors, external = [], []
                credentials = {"username": "dsc", "password": "browser-test-password"}

                def context(width=1280):
                    c = browser.new_context(http_credentials=credentials,
                                            viewport={"width": width, "height": 900},
                                            accept_downloads=True)
                    # This must work with no Internet: fail unexpected CDN requests.
                    def route(r):
                        if r.request.url.startswith(url + "/"):
                            r.continue_()
                        else:
                            external.append(r.request.url)
                            r.abort()
                    c.route("**/*", route)
                    c.on("page", lambda page: page.on("pageerror", lambda e: errors.append(str(e))))
                    return c

                desktop = context()
                page = desktop.new_page()
                page.goto(url)
                expect(page.get_by_role("button", name="Start reception", exact=True)).to_be_enabled()
                page.get_by_label("Audio centre (Hz or auto)", exact=True).fill("975")
                page.get_by_role("button", name="Start reception", exact=True).click()
                expect(page.get_by_text("Receiving", exact=True)).to_be_visible(timeout=20000)
                expect(page.get_by_text("215322000 → 004634060", exact=True)).to_be_visible(timeout=20000)
                page.get_by_role("button", name="JSON", exact=True).click()
                expect(page.get_by_text('"ecc_passed": true', exact=False)).to_be_visible()
                page.get_by_role("button", name="Close", exact=True).click()
                with page.expect_download() as download:
                    page.get_by_role("button", name="Download JSONL", exact=True).click()
                records = [json.loads(line) for line in Path(download.value.path()).read_text().splitlines()]
                assert len(records) == 1 and records[0]["ecc"] == 85

                mobile = context(390)
                phone = mobile.new_page()
                phone.goto(url)
                expect(phone.get_by_text("Receiving", exact=True)).to_be_visible()
                expect(phone.get_by_text("215322000 → 004634060", exact=True)).to_be_visible()
                expect(phone.get_by_role("button", name="Start reception", exact=True)).to_be_disabled()
                assert phone.get_by_text("Received messages", exact=True).bounding_box()["y"] < (
                    phone.get_by_text("Reception", exact=True).bounding_box()["y"])
                desktop.close()
                mobile.close()
                # All viewers have gone away. The process-owned receiver persists.
                mobile = context(390)
                phone = mobile.new_page()
                phone.goto(url)
                expect(phone.get_by_text("Receiving", exact=True)).to_be_visible()
                expect(phone.get_by_text("1 decoded", exact=True)).to_be_visible()
                assert phone.evaluate("document.documentElement.scrollWidth <= innerWidth")
                phone.get_by_role("button", name="Stop", exact=True).click()
                expect(phone.get_by_text("Stopped", exact=True)).to_be_visible()
                phone.get_by_text("Position source", exact=True).click()
                phone.get_by_label("Latitude (north positive)", exact=True).fill("50.3")
                phone.get_by_label("Longitude (east positive)", exact=True).fill("-4.2")
                phone.get_by_label("Maximum fix age (seconds)", exact=True).fill("2")
                phone.get_by_role("button", name="Read position", exact=True).click()
                expect(phone.get_by_text("Manual fix · fresh", exact=True)).to_be_visible()
                expect(phone.get_by_text("Manual fix · STALE", exact=True)).to_be_visible(timeout=10000)
                phone.get_by_role("button", name="Start reception", exact=True).click()
                expect(phone.get_by_text("2 decoded", exact=True)).to_be_visible(timeout=20000)
                phone.get_by_role("button", name="Stop", exact=True).click()
                expect(phone.get_by_text("Stopped", exact=True)).to_be_visible()
                assert phone.evaluate("document.documentElement.scrollWidth <= innerWidth")
                # Reproduce Vic's disabled-button setup. The explanation must
                # use the connected daemon, not a browser's unsaved settings.
                phone.get_by_text("Radio connection", exact=True).click()
                ptt_checkbox = phone.get_by_role("checkbox", name="Enable PTT (amateur TX)", exact=True)
                ptt_checkbox.click()
                expect(ptt_checkbox).to_have_attribute("aria-checked", "false")
                phone.get_by_role("combobox", name="Connection", exact=True).click()
                phone.get_by_role("option", name="Use an existing rigctld", exact=True).click()
                phone.get_by_role("button", name="Connect radio", exact=True).click()
                expect(phone.get_by_text("Connected", exact=True)).to_be_visible()
                phone.get_by_label("TX source address (9 digits)", exact=True).fill("232028407")
                phone.get_by_label("TX destination address (9 digits)", exact=True).fill("232028499")
                phone.get_by_label("TX DSC centre (kHz)", exact=True).fill("7071.5")
                phone.get_by_label("TX CW locator (optional; auto for position)", exact=True).fill("auto")
                readiness = phone.get_by_role("status").filter(has_text="TX unavailable")
                expect(readiness).to_contain_text("Use an existing rigctld supports reception only")
                expect(readiness).to_contain_text("Automatic locator needs a position message")
                expect(phone.get_by_role("button", name="Transmit once", exact=True)).to_be_disabled()
                phone.get_by_label("TX CW locator (optional; auto for position)", exact=True).fill("")
                expect(readiness).not_to_contain_text("Automatic locator")
                phone.get_by_role("button", name="Disconnect radio", exact=True).click()
                phone.get_by_role("combobox", name="Connection", exact=True).click()
                phone.get_by_role("option", name="Start a local rigctld", exact=True).click()
                phone.get_by_role("button", name="Connect radio", exact=True).click()
                expect(readiness).to_contain_text("PTT is disabled")
                expect(phone.get_by_role("button", name="Transmit once", exact=True)).to_be_disabled()
                phone.get_by_role("button", name="Disconnect radio", exact=True).click()
                ptt_checkbox.click()
                expect(ptt_checkbox).to_have_attribute("aria-checked", "true")
                phone.get_by_role("button", name="Connect radio", exact=True).click()
                expect(phone.get_by_text("Connected", exact=True)).to_be_visible()
                expect(phone.get_by_role("button", name="Transmit once", exact=True)).to_be_enabled()
                phone.get_by_text("Radio connection", exact=True).click()
                phone.get_by_role("button", name="Tune USB", exact=True).click()
                expect(phone.get_by_text("Last readback: 2186.525 kHz USB", exact=False)).to_be_visible()
                phone.get_by_role("button", name="Start reception", exact=True).click()
                expect(phone.get_by_text("3 decoded", exact=True)).to_be_visible(timeout=20000)
                phone.get_by_label("DSC frequency (kHz)", exact=True).fill("8414.5")
                phone.get_by_role("button", name="Tune USB", exact=True).click()
                expect(phone.get_by_text("Last readback: 8413.525 kHz USB", exact=False)).to_be_visible()
                expect(phone.get_by_text("4 decoded", exact=True)).to_be_visible(timeout=20000)
                with phone.expect_download() as download:
                    phone.get_by_role("button", name="Download JSONL", exact=True).click()
                records = [json.loads(line) for line in Path(download.value.path()).read_text().splitlines()]
                assert records[-2]["rig"]["frequency_hz"] == 2187500
                assert records[-1]["rig"]["frequency_hz"] == 8414500
                assert records[-2]["receiver_session"] != records[-1]["receiver_session"]
                # TX is explicit, allocation checked and serialized with RX.
                phone.get_by_label("TX source address (9 digits)", exact=True).fill("891234567")
                phone.get_by_label("TX destination address (9 digits)", exact=True).fill("897654321")
                phone.get_by_label("TX DSC centre (kHz)", exact=True).fill("2187.5")
                expect(phone.get_by_role("button", name="Transmit once", exact=True)).to_be_disabled()
                phone.get_by_label("TX DSC centre (kHz)", exact=True).fill("7101.7")
                phone.get_by_role("button", name="Transmit once", exact=True).click()
                expect(phone.get_by_text("Transmitting CW identification and DSC", exact=True)).to_be_visible()
                expect(phone.get_by_role("button", name="Tune USB", exact=True)).to_be_disabled()
                expect(phone.get_by_text("Complete; PTT off confirmed", exact=True)).to_be_visible()
                expect(phone.get_by_text("Receiving", exact=True)).to_be_visible()
                phone.get_by_role("button", name="Last TX JSON", exact=True).click()
                expect(phone.get_by_text('"ptt_off_confirmed": true', exact=False)).to_be_visible()
                phone.get_by_role("button", name="Close", exact=True).click()
                phone.get_by_role("button", name="Transmit once", exact=True).click()
                expect(phone.get_by_text("Transmitting CW identification and DSC", exact=True)).to_be_visible()
                phone.get_by_role("button", name="Abort TX", exact=True).click()
                expect(phone.get_by_text("Transmission cancelled or timed out", exact=True).first).to_be_visible()
                phone.get_by_role("button", name="Disconnect radio", exact=True).click()
                expect(phone.get_by_text("Disconnected", exact=True)).to_be_visible()
                expect(phone.get_by_text("Stopped", exact=True).first).to_be_visible()
                # Original IC-706 with a separate Digirig RTS port; no CAT PTT.
                phone.get_by_text("Radio connection", exact=True).click()
                phone.get_by_label("Hamlib model number", exact=True).fill("3009")
                phone.get_by_label("Radio serial port (COM3, /dev/ttyUSB0, …)", exact=True).fill("COM-TEST-CAT")
                phone.get_by_role("combobox", name="PTT method", exact=True).click()
                phone.get_by_role("option", name="Serial RTS (Digirig Mobile)", exact=True).click()
                phone.get_by_label("PTT serial port (blank = radio port)", exact=True).fill("COM-TEST-PTT")
                phone.get_by_role("button", name="Connect radio", exact=True).click()
                expect(phone.get_by_text("Connected", exact=True)).to_be_visible()
                phone.get_by_role("button", name="Transmit once", exact=True).click()
                expect(phone.get_by_text("Complete; Hamlib reports PTT line off", exact=True)).to_be_visible()
                phone.get_by_role("button", name="Last TX JSON", exact=True).click()
                expect(phone.get_by_text('"ptt_method": "RTS"', exact=False)).to_be_visible()
                expect(phone.get_by_text('"ptt_confirmation": "serial_control_line"', exact=False)).to_be_visible()
                phone.get_by_role("button", name="Close", exact=True).click()
                phone.get_by_role("button", name="Disconnect radio", exact=True).click()
                expect(phone.get_by_text("Disconnected", exact=True)).to_be_visible()
                phone.get_by_text("Save station settings", exact=True).click()
                phone.get_by_label("Profile name", exact=True).fill("saved-test")
                phone.get_by_role("button", name="Save station", exact=True).click()
                expect(phone.get_by_text("Station saved", exact=True)).to_be_visible()
                phone.get_by_label("DSC frequency (kHz)", exact=True).fill("2187.5")
                phone.get_by_role("button", name="Load station", exact=True).click()
                expect(phone.get_by_label("DSC frequency (kHz)", exact=True)).to_have_value("8414.500")
                phone.reload()
                expect(phone.get_by_label("DSC frequency (kHz)", exact=True)).to_have_value("8414.500")
                expect(phone.get_by_role("combobox", name="Saved station", exact=True)).to_have_value("saved-test")
                phone.get_by_text("Radio connection", exact=True).click()
                expect(phone.get_by_label("Hamlib model number", exact=True)).to_have_value("3009")
                expect(phone.get_by_role("combobox", name="PTT method", exact=True)).to_have_value("Serial RTS (Digirig Mobile)")
                expect(phone.get_by_label("PTT serial port (blank = radio port)", exact=True)).to_have_value("COM-TEST-PTT")
                phone.get_by_text("Radio connection", exact=True).click()
                screenshot_dir = os.environ.get("DSC_SCREENSHOT_DIR")
                if screenshot_dir:
                    expect(phone.get_by_label("Latitude (north positive)", exact=True)).to_be_hidden()
                    phone.evaluate("window.scrollTo({top: 0, behavior: 'instant'})")
                    Path(screenshot_dir).mkdir(parents=True, exist_ok=True)
                    phone.screenshot(path=str(Path(screenshot_dir) / "phone.png"), full_page=True, animations="disabled")
                    phone.set_viewport_size({"width": 1280, "height": 900})
                    phone.evaluate("window.scrollTo({top: 0, behavior: 'instant'})")
                    phone.screenshot(path=str(Path(screenshot_dir) / "desktop.png"), full_page=True, animations="disabled")
                assert not errors, errors
                assert not external, external
                browser.close()
            print("Browser smoke passed: authentication, decode, JSON, shared state, reconnect, position, rig tune/readback, retune while receiving, TX disabled reasons/external daemon/PTT opt-in/locator validation/completion/abort, profiles, phone layout, offline assets.")
        except Exception:
            log.seek(0)
            print(log.read())
            raise
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


if __name__ == "__main__":
    main()

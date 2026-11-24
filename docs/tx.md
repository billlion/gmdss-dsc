# Amateur CW + DSC transmission

This first CAT transmitter supports the **IC-7100, Hamlib model 3070**, and
Hamlib's dummy radio (model 1). It starts and owns a local `rigctld` so the
backend, PTT method and cache settings are known. Existing external daemons and
other radio models remain available for RX. In particular, the marine Icom
backends need separate verification of their TX frequency before enabling PTT.

## IC-7100 setup

Vic has reported successful NMEA input, IC-7100 RX control and an audio test
decoded by an IC-M803. That bench test used an 8 MHz DSC channel with the
IC-7100 feeding a dummy load; "6 m" referred to the M803's receiving antenna.
The new automatic PTT sequence still needs his hardware test on an amateur frequency.

1. Use the working radio serial port/baud/CI-V address in the saved `home`
   profile. Disconnect AirMail or any other program owning that serial port.
2. Select the radio's **output** audio device with `dsc devices`. Windows may
   show separate input and output entries called USB Audio CODEC. The input
   selected for RX is not automatically selected for TX.
3. This implementation uses ordinary **USB**, not USB-D/PKTUSB. On the IC-7100
   set **SET → Connectors → DATA OFF MOD → USB**. Adjust **USB MOD Level** and
   the computer playback level for clean modulation. The radio's RF power is
   set manually; `level = 0.25` below scales audio amplitude, not RF power.
4. Turn **SPLIT, DUP and VOX off**. TX explicitly selects **VFO A** and leaves
   the radio there. The IC-7100 has RIT but no XIT; no receive offset is used
   to calculate the transmit frequency. Do not operate the tuning controls
   or another CAT client during a burst.
5. Enable CAT PTT in the station's radio settings, then reconnect. Start with
   a low-power dummy-load test. Keep the radio's own timeout timer enabled.

The modulation settings are described in the
[Icom IC-7100 manual](https://icomuk.co.uk/files/icom/PDF/productManual/IC-7100_ENG_Paper_0.pdf),
page 1-15. The application does not change those audio-routing menus.

## Saved settings and CLI

Create a profile with `dsc profiles init` if needed; `dsc profiles` prints its
file location. For an existing `home` profile add `allow_ptt = true` to its
**existing** `[profiles.home.rig]` table. Do not duplicate the table header.
All new and old profiles default to `allow_ptt = false`.

Add a TX table (replace the example callsign and device with your own):

```toml
[profiles.home.tx]
band_plan = "uk"
callsign = "M0ABC"
output_device = "USB Audio CODEC"
output_channel = 1
level = 0.25
lead_s = 0.2
tail_s = 0.2
```

The `uk` selection checks UK allocation edges for 160, 80, 60, 40, 30, 20,
17, 15, 12, 10 and 6 metres. It does not choose preferred digital sub-bands.
The eleven separate 60 m blocks are checked individually; their intervening
gaps are blocked. The plan uses the Full licence frequency table; the operator
still chooses a frequency, power and location appropriate to their privileges.
In particular, UK 60 m has additional licence and location conditions.
Source: [Ofcom OFW611, Table C and Note G](https://www.ofcom.org.uk/siteassets/resources/documents/spectrum/emf/emf-amateur-licence-terms-and-conditions.pdf),
checked 23 November 2026. No wider custom range or bypass is provided.

First inspect a plan without opening the radio or audio device:

```sh
dsc transmit-test --profile home --source 891234567 --destination 897654321 --frequency-khz 7101.7 --dry-run
```

Then omit `--dry-run` to transmit **once**. Replace the example addresses with
those agreed for the experiment. Both addresses use the optional project
`89` + seven-digit DMR-ID convention; they are not allocated maritime MMSIs.

`--frequency-khz` always means the **RF FSK centre**, not the USB dial. The
example gives a 7100.000 kHz USB dial and a 7101.700 kHz RF centre, using 1700 Hz
audio. There is no implicit use of the profile's marine RX frequency.

For a position acknowledgement, using the profile's position source:

```sh
dsc transmit-position --profile home --source 891234567 --destination 897654321 --frequency-khz 7101.7 --locator auto
```

Position options work as in `encode-position`; explicit options override the
profile. A new fix is read for each message and its age checked again before
PTT. `--locator auto` includes its Maidenhead locator in the CW prefix.
`--callsign`, `--band-plan uk`, `--device`, `--output-channel`, and `--level`
can override the saved TX settings. `--centre` overrides the profile audio
centre; TX accepts 700–2300 Hz. `--cw-wpm` defaults to 18. Test calls support
`--ack`. There are no distress, arbitrary frame or WAV-file CAT TX commands.

Progress goes to stderr; successful completion prints JSON to stdout,
including the actual frequency readback and `ptt_off_confirmed`. Ctrl+C stops
playback and attempts to release PTT. A failed or interrupted burst is never
automatically repeated.

## Browser

In **Radio connection**, enable **CAT PTT** before connecting the IC-7100.
In **Amateur transmission**, select the UK allocation plan and enter the
callsign, output device, source, destination and **TX DSC centre**. Use
**Save station** to retain the TX audio, callsign and allocation settings.
Message addresses and the TX frequency must be entered explicitly each visit.

**Transmit once** pauses reception, prepares one message, tunes, checks and
keys the radio, plays CW + DSC, then confirms PTT off. Successful completion
resumes reception if it was running, with a fresh decoder at the **selected
amateur frequency**. It does not return to the previous marine RX channel.
**Abort TX** is available to all connected viewers. After an error or abort,
reception stays stopped so the operator can check the result. The last
successful TX JSON is available on the page until another attempt.

Radio and audio operations are serialized across browsers. Closing a browser
does not cancel a burst: the server owns the bounded job. Use Abort TX to stop
it. Server shutdown requests cancellation before closing the owned helper.
The existing LAN access password applies to both RX and TX controls.

### If Transmit once is greyed out

The text immediately above the button now explains the blocking settings.
In particular, a radio can show **Connected** and tune successfully while TX
remains unavailable through **Use an existing rigctld**. For this first TX
version:

1. Click **Disconnect radio**. This closes our connection; it does not stop a
   separately launched `rigctld`.
2. Stop that separate `rigctld` process so it releases the radio's serial port.
3. Select **Start a local rigctld** in **Radio connection**. For the IC-7100,
   enter model **3070** and the serial port, baud and CI-V address that worked
   previously. The radio description is just a label; "Hamlib dummy" there
   does not identify the backend of an external daemon.
4. Tick **Enable CAT PTT**, then **Connect radio**. If you were already
   connected, disconnect before changing that setting.
5. Select the UK TX plan, enter the callsign, radio output device, source and
   destination addresses, and an amateur TX centre frequency. Save the station
   to retain the connection and TX settings for next time.

For **Test call**, the CW locator must be blank or explicit (for example,
`IO70VH`). **auto** requires **Position acknowledgement**, which reads a fresh
fix. These form errors are shown before the button becomes available.

**Radio output device** is the sound device ID/name from **List output
devices**. **Output channel** is a channel *within* that device: normally
**1** for mono/left, or **2** for right on a stereo device. It is not another
device ID. Channel availability is checked when opening the output, before
PTT; the form does not repeatedly open/probe sound hardware while idle.

## Checks and limits

Before keying, the program validates addresses, requires a CW callsign and
explicit output device/plan, opens and prepares the audio stream, selects
VFO A, rejects active PTT, split, repeater shift or VOX, sets USB, and reads
back the frequency/mode. The dummy additionally rejects nonzero XIT.
Unsupported/failed readbacks block transmission.

The entire conservative **3 kHz USB window above the suppressed carrier**
must fit inside one allocation. This covers the CW tone and both FSK tones
with margin and also keeps the dial inside the allocation. It is stricter
than just testing the FSK centre and is not a measurement of transmitter
spectral purity. RF amplification, filters, audio clipping and unwanted
emissions remain properties of the actual station.

The prepared waveform is limited to 90 seconds including lead, tail and a
five-second watchdog allowance. An independent worker attempts PTT-off on
cancellation or timeout, even if audio playback stalls. A normal finish waits
for the output buffer to drain before the tail and key-off. Cleanup releases
PTT **before** closing the audio driver. A fresh TCP connection is used for
key-off recovery if the key-on reply was lost; only release commands are
retried, never the burst.

If PTT-off cannot be confirmed, the error says **PTT OFF UNCONFIRMED — stop the
transmitter locally**. TX is blocked, reception stays stopped, and the owned
helper is retained for recovery. Software cannot guarantee key-off after a
broken serial link, a stopped server/OS or lost power: the radio timeout and
local stop control provide independent protection.

Tests include allocation edges/gaps, CW/DSC round trips, an actual Hamlib dummy
PTT cycle, lost acknowledgements, failed release, audio failures, cancellation,
timeouts, shared RX/TX exclusion and browser interactions. They do not replace
the IC-7100 hardware test.

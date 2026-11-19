# fanCalibrator

Finds the **quietest combination of CPU, GPU and case fan speeds that keeps
every component under its temperature target**, then generates fan curves and
runs them.

The goal is *not* "as cold as possible". Past a certain speed (the knee),
extra RPM buys very little cooling while noise keeps climbing
(~+15 dB per doubling of RPM). So the tool holds temps a little under your
targets and spends no more noise than that takes.

## The three arms

| Arm  | Controls       | Driven by                    |
|------|----------------|------------------------------|
| cpu  | CPU cooler fan | CPU temperature              |
| gpu  | GPU fan        | GPU temperature              |
| case | case fans      | max(CPU-, GPU-based demand)  |

Case fans don't cool either chip directly. They lower the air temperature both
coolers draw in, so slowing them raises CPU **and** GPU temps. The arms are
tuned **jointly**, because tuning each fan on its own misses trades like
"10% more case airflow lets the louder GPU fan drop 8%".

## How calibration works

1. **Characterize each fan** at idle: PWM→RPM curve, *stall* duty (lowest
   duty a spinning fan survives) and *start* duty (lowest duty that restarts
   a stopped fan).
2. **Idle temps** with every arm at its floor (stall duty + margin).
3. **Sustained CPU+GPU load**, then a measured search starting from all arms
   at 100%:
   * **Descend:** lower the arm whose next step saves the most noise per °C
     of headroom used. Every step is a real steady-state measurement
     (settling is detected from the temperature slope, not a fixed wait).
   * **Knee stop:** once a step saves less than `min_db_per_degree`
     (default 0.05 dB per °C), that arm stays where it is.
   * **Exchange:** raise one arm so another can drop by as much as the
     measured sensitivities allow. Kept only if it's measurably quieter and
     still within targets.
4. **Curves**: floor at idle temp, the optimized duty at the measured
   full-load temp, then 100% by target + 3 °C.

The noise model uses fan laws and per-fan `max_db` (a datasheet figure, or a
weighting for a fan that's closer to your ear). Without `max_db`, every fan is
assumed equally loud at its max RPM.

## Runtime control

* EMA smoothing (ignores 1-second CPU spikes)
* Hysteresis: speeds up on `curve(T)`, only slows down on `curve(T + 3 °C)`
* Fast up / slow down rate limits
* Zero-RPM arms get kicked at their start duty when they restart
* Full speed on any sensor read error or ≥ 95 °C
* Restores the firmware's automatic control on exit

## Usage

Try it without hardware:

```bash
python3 -m fancalibrator.cli calibrate --sim -o profile.json
python3 -m fancalibrator.cli run --sim profile.json --duration 600
```

On real hardware (Linux, root needed to write PWM):

```bash
python3 -m fancalibrator.cli list-hwmon
sudo python3 -m fancalibrator.cli calibrate my-config.json -o profile.json
sudo python3 -m fancalibrator.cli run my-config.json profile.json
```

Copy `config.example.json` and point it at your hwmon paths. The load
command should stress the **CPU and GPU together**. Without a load command, the
tool pauses and lets you start the load yourself. Expect a full calibration
to take one to two hours, because each search step waits for temperatures to
settle.

Notes:
* NVIDIA GPU fans aren't exposed through hwmon. Leave that arm out, or
  implement the small `Fan` protocol in `hardware.py` (e.g. via
  `nvidia-settings`). AMD GPUs expose `pwm1` under their hwmon device.
* For an AIO or custom loop, add a coolant temperature sensor and have the
  radiator fans' arm control on it rather than on CPU temperature.
* Room temperature shifts the result. Calibrate in typical conditions, or
  re-run seasonally. The 2 °C guard band absorbs small swings.

## Tests

```bash
python3 -m pytest
```

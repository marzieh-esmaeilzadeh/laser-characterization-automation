"""Two-stage PM100D measurement: pump absorption reference and laser output."""
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
import csv
import time

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import pyvisa

BASE = Path(__file__).resolve().parent / 'Laser_Experiment'
BASE.mkdir(exist_ok=True)
REFERENCE = BASE / 'Pump_Absorption_Reference.csv'
PUMP_WAVELENGTH_NM = 912.0
OUTPUT_WAVELENGTH_NM = 1064.0  # Change to actual laser wavelength.
SAMPLES = 5
SAMPLE_DELAY_S = 0.2


def current_key(value):
    """Canonical decimal representation; no approximate matching."""
    try:
        number = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError('Invalid current') from exc
    if not number.is_finite() or number < 0:
        raise ValueError('Current must be finite and nonnegative')
    return format(number.normalize(), 'f')


def ask_currents():
    print('Enter diode currents one at a time. Type done to finish.')
    currents = []
    while True:
        raw = input('Current (A), or done: ').strip()
        if raw.lower() == 'done':
            if currents:
                return currents
            print('Enter at least one current.')
            continue
        try:
            key = current_key(raw)
        except ValueError as exc:
            print(exc)
            continue
        if key in currents:
            print('Already entered; duplicates are not allowed.')
            continue
        currents.append(key)


def connect_meter():
    rm = pyvisa.ResourceManager()
    for resource in rm.list_resources():
        device = None
        try:
            device = rm.open_resource(resource)
            device.timeout = 10000
            identity = device.query('*IDN?').strip()
            if 'THORLABS' in identity.upper() and 'PM100D' in identity.upper():
                print('Connected:', identity)
                return rm, device
        except Exception:
            pass
        if device is not None:
            try:
                device.close()
            except Exception:
                pass
    rm.close()
    raise RuntimeError('PM100D not found. Check USB connection and NI-VISA.')


def measure(meter, wavelength_nm):
    meter.write(f'SENS:CORR:WAV {wavelength_nm}')
    values = []
    for _ in range(SAMPLES):
        value = float(meter.query('MEAS:POW?'))
        if not np.isfinite(value):
            raise ValueError('Power meter returned a non-finite reading')
        values.append(value)
        time.sleep(SAMPLE_DELAY_S)
    return float(np.mean(values))


def confirm_position(label, wavelength):
    print(f'\nPrepare the sensor for {label} ({wavelength:g} nm).')
    print('Block/disable the beam safely before moving the sensor.')
    answer = input('When safely positioned, type READY (or q to stop): ').strip().lower()
    if answer == 'q':
        raise KeyboardInterrupt('Measurement cancelled by user')
    if answer != 'ready':
        raise ValueError('Position not confirmed. Measurement stopped.')


def run_reference(meter):
    if REFERENCE.exists():
        answer = input('Reference exists. Replace it? Type REPLACE: ').strip()
        if answer != 'REPLACE':
            print('Existing reference preserved.')
            return
    currents = ask_currents()
    print('Use the SAME currents and optical setup in both passes.')
    readings = {key: {'Current (A)': key} for key in currents}
    stages = [
        ('P_incident (W)', 'INCIDENT pump power', PUMP_WAVELENGTH_NM),
        ('P_transmitted (W)', 'TRANSMITTED pump power', PUMP_WAVELENGTH_NM),
    ]
    for column, label, wavelength in stages:
        confirm_position(label, wavelength)
        for key in currents:
            input(f'Set diode current to {key} A, then press Enter to measure...')
            power = measure(meter, wavelength)
            readings[key][column] = power
            print(f'{column} at {key} A: {power:.8g} W')
    rows = []
    for key in currents:
        row = readings[key]
        row['P_absorbed (W)'] = row['P_incident (W)'] - row['P_transmitted (W)']
        rows.append(row)
    df = pd.DataFrame(rows)
    # Never replace a valid reference with nonphysical absorbed power.
    if (df['P_absorbed (W)'] <= 0).any():
        problem = df.loc[df['P_absorbed (W)'] <= 0, 'Current (A)'].tolist()
        print('WARNING: Nonpositive absorbed power at currents:', problem)
        print('Reference was NOT replaced. Check readings and geometry.')
        return
    df.to_csv(REFERENCE, index=False)
    print('\nReference saved:', REFERENCE)
    print(df.to_string(index=False))


def load_reference():
    if not REFERENCE.exists():
        raise FileNotFoundError(f'Create a reference first: {REFERENCE}')
    # Read current as text to retain decimal matching.
    ref = pd.read_csv(REFERENCE, dtype={'Current (A)': str})
    needed = ['Current (A)', 'P_incident (W)', 'P_transmitted (W)', 'P_absorbed (W)']
    if any(c not in ref.columns for c in needed):
        raise ValueError('Reference CSV has missing columns.')
    ref['key'] = ref['Current (A)'].map(current_key)
    if ref['key'].duplicated().any():
        raise ValueError('Reference contains duplicate currents.')
    for col in needed[1:]:
        ref[col] = pd.to_numeric(ref[col], errors='raise')
    if not np.isfinite(ref[needed[1:]].to_numpy(dtype=float)).all():
        raise ValueError('Reference contains invalid numeric values.')
    if (ref['P_absorbed (W)'] <= 0).any():
        raise ValueError('Reference contains nonpositive absorbed power.')
    return ref.set_index('key')


def run_output(meter):
    ref = load_reference()
    print('\nAvailable reference currents (A):', ', '.join(ref.index))
    print('Only exact currents are accepted; no interpolation.')
    confirm_position('LASER OUTPUT', OUTPUT_WAVELENGTH_NM)
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S_%f')
    output_csv = BASE / f'Laser_Output_{timestamp}.csv'
    output_plot = BASE / f'Laser_Output_{timestamp}.png'
    rows = []
    while True:
        raw = input('\nCurrent (A), or q to finish: ').strip()
        if raw.lower() == 'q':
            break
        try:
            key = current_key(raw)
        except ValueError as exc:
            print(exc)
            continue
        if key not in ref.index:
            print('Current not in reference. Choose:', ', '.join(ref.index))
            continue
        if any(r['Current (A)'] == key for r in rows):
            print('Current already measured in this run.')
            continue
        input(f'Set diode current to {key} A, then press Enter to measure...')
        power = measure(meter, OUTPUT_WAVELENGTH_NM)
        absorbed = float(ref.loc[key, 'P_absorbed (W)'])
        row = {
            'Current (A)': key,
            'P_incident (W)': float(ref.loc[key, 'P_incident (W)']),
            'P_transmitted (W)': float(ref.loc[key, 'P_transmitted (W)']),
            'P_absorbed (W)': absorbed,
            'P_output (W)': power,
            'Optical efficiency (%)': 100 * power / absorbed,
        }
        rows.append(row)
        pd.DataFrame(rows).to_csv(output_csv, index=False)
        print(f'Output: {power:.8g} W | Absorbed: {absorbed:.8g} W')
        print('Saved:', output_csv)
    if not rows:
        print('No output measurements were recorded.')
        return
    df = pd.DataFrame(rows)
    print('\nMeasurements:\n', df.to_string(index=False))
    if len(df) < 2 or df['P_absorbed (W)'].nunique() < 2:
        print('At least two distinct absorbed-power values are needed for a fit.')
        return
    print('\nSlope efficiency must be fitted over the LINEAR above-threshold region.')
    print('Measured currents:', ', '.join(df['Current (A)']))
    raw = input('Currents to include in linear fit, comma-separated (or skip): ').strip()
    if raw.lower() == 'skip' or not raw:
        print('Fit skipped. CSV saved.')
        return
    try:
        chosen = [current_key(v.strip()) for v in raw.split(',')]
    except ValueError:
        print('Invalid fit currents; fit skipped.')
        return
    if len(chosen) != len(set(chosen)) or any(k not in set(df['Current (A)']) for k in chosen):
        print('Fit currents must be unique and already measured; fit skipped.')
        return
    selected = df[df['Current (A)'].isin(chosen)]
    if len(selected) < 2 or selected['P_absorbed (W)'].nunique() < 2:
        print('Need at least two distinct absorbed-power points; fit skipped.')
        return
    x = selected['P_absorbed (W)'].to_numpy(float)
    y = selected['P_output (W)'].to_numpy(float)
    slope, intercept = np.polyfit(x, y, 1)
    print(f'Slope efficiency for selected region: {100*slope:.2f}%')
    plt.figure(figsize=(8, 5))
    plt.scatter(df['P_absorbed (W)'], df['P_output (W)'], label='Measured output')
    fit_x = np.linspace(x.min(), x.max(), 100)
    plt.plot(fit_x, slope * fit_x + intercept, label=f'Linear fit ({100*slope:.2f}%)')
    plt.xlabel('Absorbed pump power (W)')
    plt.ylabel('Laser output power (W)')
    plt.title('Laser output vs absorbed pump power')
    plt.grid(True)
    plt.legend()
    plt.tight_layout()
    plt.savefig(output_plot, dpi=300)
    print('Plot saved:', output_plot)
    plt.show()


def main():
    print('Thorlabs PM100D — two-stage laser measurement')
    print('1: Create pump absorption reference')
    print('2: Measure laser output using reference')
    mode = input('Choose 1 or 2 (or q): ').strip().lower()
    if mode == 'q':
        return
    if mode not in ('1', '2'):
        print('Invalid mode.')
        return
    if mode == '2':
        # Check reference before connecting hardware.
        load_reference()
    rm, meter = connect_meter()
    try:
        if mode == '1':
            run_reference(meter)
        else:
            run_output(meter)
    finally:
        meter.close()
        rm.close()
        print('Power meter connection closed.')


if __name__ == '__main__':
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        print('\nStopped. Any previously saved output rows remain in CSV.')
    except Exception as exc:
        print(f'ERROR: {exc}')
        print('Check instrument, sensor, settings, and saved files.')

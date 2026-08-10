"""Checks the CRAC power model behaves like the physics it claims.

The coefficients in twins/energy_twin.py are derived, not measured, so what
can be tested is whether the model obeys the laws it says it obeys and lands
in a physically credible range. A PUE that reads 1.16 is exactly the kind of
number that passes silently into an ROI slide, so it is asserted here.

    python3 tests/test_energy_model.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from twins.energy_twin import (
    crac_power_kw, P_FAN_RATED_KW, P_COMP_RATED_KW,
    FAN_RPM_RATED, NOMINAL_AIRFLOW_M3_S, FAN_TOTAL_PRESSURE_PA,
    FAN_COMBINED_EFFICIENCY, RATED_COOLING_KW, RATED_COP,
)

failures = []


def check(label, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}" + (f"   [{detail}]" if detail else ""))
    if not cond:
        failures.append(label)


def main():
    print("\n1. Coefficients follow from their stated derivation:")
    expected_fan = NOMINAL_AIRFLOW_M3_S * FAN_TOTAL_PRESSURE_PA / FAN_COMBINED_EFFICIENCY / 1000
    check("P_FAN_RATED == Q·dp/eta", abs(P_FAN_RATED_KW - expected_fan) < 0.002,
          f"{P_FAN_RATED_KW} vs {expected_fan:.3f} kW")
    check("P_COMP_RATED == Q_rated/COP",
          abs(P_COMP_RATED_KW - RATED_COOLING_KW / RATED_COP) < 1e-9,
          f"{P_COMP_RATED_KW} kW")

    print("\n2. Fan power obeys the cube law:")
    f_base, _ = crac_power_kw(FAN_RPM_RATED, 0)
    check("at rated speed, fan power == rated", abs(f_base - P_FAN_RATED_KW) < 0.002,
          f"{f_base} kW")
    f_double, _ = crac_power_kw(FAN_RPM_RATED * 2, 0)
    check("doubling speed multiplies power by 8", abs(f_double / f_base - 8.0) < 0.01,
          f"{f_double / f_base:.3f}x")
    f_half, _ = crac_power_kw(FAN_RPM_RATED * 0.5, 0)
    check("halving speed divides power by 8", abs(f_half / f_base - 0.125) < 0.001,
          f"{f_half / f_base:.4f}x")

    # The claim made in the README and on the dashboard.
    f_lo, _ = crac_power_kw(3200, 0)
    f_hi, _ = crac_power_kw(3450, 0)
    rise = (f_hi / f_lo - 1) * 100
    check("3200->3450 rpm gives a ~25% power rise", 24.0 < rise < 26.0, f"{rise:.1f}%")

    print("\n3. Compressor power is linear in load fraction:")
    _, c50 = crac_power_kw(0, 50)
    _, c100 = crac_power_kw(0, 100)
    _, c25 = crac_power_kw(0, 25)
    check("100% load == rated", abs(c100 - P_COMP_RATED_KW) < 1e-6, f"{c100} kW")
    check("50% load == half of rated", abs(c50 - P_COMP_RATED_KW / 2) < 1e-6, f"{c50} kW")
    check("linear: c50 - c25 == c100 - c75",
          abs((c50 - c25) - (crac_power_kw(0, 100)[1] - crac_power_kw(0, 75)[1])) < 1e-6)

    print("\n4. Missing telemetry produces zero, never an invented number:")
    check("no rpm -> 0 fan kW", crac_power_kw(None, 55)[0] == 0.0)
    check("no compressor load -> 0 comp kW", crac_power_kw(3200, None)[1] == 0.0)

    print("\n5. PUE lands in a credible range across the whole fault:")
    rows = [("healthy", 3200, 55.0, 18.6), ("mid", 3330, 70.0, 21.0),
            ("tripped", 3450, 85.0, 23.4)]
    pues = []
    for label, rpm, comp, it in rows:
        f, c = crac_power_kw(rpm, comp)
        pue = (it + f + c) / it
        pues.append(pue)
        check(f"{label}: PUE in 1.25-1.60", 1.25 <= pue <= 1.60, f"PUE {pue:.3f}")
    check("PUE worsens monotonically as the CRAC degrades",
          pues[0] < pues[1] < pues[2],
          " -> ".join(f"{p:.3f}" for p in pues))
    check("the old placeholder's PUE (~1.16) is no longer producible",
          min(pues) > 1.20, f"min PUE {min(pues):.3f}")

    print("\n6. Degradation has a measurable cost (the ROI quantity):")
    f0, c0 = crac_power_kw(3200, 55.0)
    f1, c1 = crac_power_kw(3450, 85.0)
    delta = (f1 + c1) - (f0 + c0)
    check("degraded CRAC draws >3 kW more than healthy", delta > 3.0, f"{delta:.2f} kW")
    check("fan contributes a non-trivial share of that", (f1 - f0) / delta > 0.08,
          f"fan {(f1 - f0):.2f} kW of {delta:.2f} kW")

    print()
    if failures:
        print(f"{len(failures)} assertion(s) failed: {failures}")
        return 1
    print("All energy-model assertions passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

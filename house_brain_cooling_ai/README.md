# House Brain Cooling AI

The cooling plant's shadow twin (AI family plan step 6, design R1). It learns how the house actually
cools in each plant mode (idle drift, vent, wet evap and A/C), makes its own plan each minute from
those learned rates and the Arbiter's own targets and permission flags, and counts how that plan
compares with what the house actually did. The comfort and cost differences it shows are
SIMULATED and are never presented as savings.

It has no hands: no POST anywhere, no Home Assistant state write, no service call, Supervisor role
`default`. It writes no Home Assistant sensor or helper, so no live code can read it as a control.
Python standard library only; no private settings.

See [DOCS.md](DOCS.md).

# House Brain Water AI

The water member of the House Brain AI family (plan step 8, design R1). It learns the RO tank in
psi (refills, draws, overnight bleed, time to full) and keeps a ledger of the yard hose timers
(runs, Orbit-reported gallons, the schedule kept, battery dates). Leak sensors, a rain or ET0
source, indoor plant watering and the offline timers are named slots, used once their hardware
or data exists.

It has no hands: no POST anywhere, no Home Assistant state write, no service call, Supervisor role
`default`. B-hyve keeps watering control. It raises no alerts of its own. Python standard library
only; no private settings.

See [DOCS.md](DOCS.md).

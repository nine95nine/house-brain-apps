# House Brain Pool AI

The pool member of the House Brain AI family (plan step 7, design R1). It learns the pool from the
house's own history: how fast this pool uses chlorine (from the water tests you log), how well the salt
cell is working over time and when it will need cleaning or replacing, how many days until salt falls
below 3,200 ppm, and what each pump run costs when Sense can see it. It suggests the day a next test
would help most, without reminders. Water temperature is marked missing until a trusted sensor exists;
temperature-dependent demand, LSI/CSI, Ondilo local data and the #111 pump protections are named slots,
built later.

It has no hands: no POST anywhere, no Home Assistant state write, no service call, no Supervisor API.
It never touches the AquaRite or the pump, raises no alerts, and runs in shadow beside the AquaRite
packages. Python standard library only; no private settings.

See [DOCS.md](DOCS.md).

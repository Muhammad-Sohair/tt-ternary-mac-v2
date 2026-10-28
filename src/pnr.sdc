# PnR-only constraints: LibreLane's base SDC plus one deliberate over-constraint.
# Signoff STA reads signoff.sdc (base SDC only), so the signoff numbers are not affected by this.
source $::env(SCRIPTS_DIR)/base.sdc

# Net _1543_ (driver _4435_, a21oi): decoder code bit for column 3, loads w[3]/w[8]/w[13]/w[18].
# It ended at 0.8005 ns vs 0.75 ns at max_ss in the run-a build, because design repair only sees
# estimated parasitics. OpenSTA has no per-net max_transition, so the net's load is over-constrained
# instead: set_load on a net takes precedence over its parasitics in delay calculation.
# 0.0354 pF = 115 % of the net's max-RC extracted wire cap in run a (0.0308 pF).
set_load 0.0354 [get_nets _1543_]

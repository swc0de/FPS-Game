"""Milestone 8 bot AI ("v2"), selectable per team next to the Milestone 5 brain.

Layers (docs/OVERHAUL_PLAN.md, section 3):

* ``strategy``      team plan, roles and mid-round calls (TeamStrategy)
* ``brain``         per bot: utility-scored actions with commitment (BrainV2)
* ``controllers``   movement, combat micro and aim policy on top of the
                    shared AimController / Intent / character layers
* ``knowledge``     facts with provenance, radio callouts, radar glances
* ``belief``        where unseen enemies can be by now (possibility field)
* ``tactical_map``  precomputed tactical points, visibility, cover, spots
"""

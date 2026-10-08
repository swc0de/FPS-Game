# Milestone 9 results: realistic soldiers

Measured on branch `claude/upbeat-maxwell-p1h773-soldiers` against the Phase 0 baseline
([`docs/baseline/soldier_stats.json`](../baseline/soldier_stats.json), the before sheet
[`docs/images/soldiers_before.jpg`](../images/soldiers_before.jpg)) and the acceptance criteria
in [OVERHAUL_PLAN.md](../OVERHAUL_PLAN.md) section 7. Statistics: [`soldier_stats.json`](soldier_stats.json)
(`tools/soldier_sheet.py --procedural --stats ...`).

![Soldiers after](../images/soldiers_after.jpg)

*The same shots as the before sheet, plus faces at 0.9 m (the last panel). Vanguard (attack) is
on the left of each pair, Bastion (defence) on the right.*

## Summary

| criterion | target | result | met |
|---|---|---|---|
| skinning | linear blend, 4 weights, in every pass | 47-bone game skeleton, `mat3x4` palette (576 vertex-uniform components), 4 weights per vertex; the main pass, the depth pre-pass and every shadow pass use the same skinning | yes |
| levels of detail | about 12k / 4k / 1.2k triangles | 11.6-11.7k / 3.8-3.9k / 1.19-1.22k for full kits (balaclava kits 9.6k / 3.2k / 1.0k) | yes |
| draw calls per soldier, weapon included | ≤ 6 at LOD0, ≤ 3 far | **5** at LOD0 (body 1, rifle 4), **2** far (body 1, rifle 1) | yes |
| animation | procedural, with events; ragdoll | reload (the weapon's own reload time), switch, throw, knife, plant and defuse (looping), hit flinch (same at any health); Bullet ragdoll on death, visual only | yes |
| animation LOD | | the palette every tick within 14 m, every second tick to 40 m, every fourth beyond or behind the camera; the hit boxes are posed every tick | yes |
| animation CPU for 10 soldiers | reported | being measured on a quiet machine | reported |
| no seams or candy-wrapper | twist ±90°: rings keep ≥ 70 % | 85-95 % for the forearm and the upper arm (tests); pieces are closed meshes, the head's open neck sits inside the collar | yes |
| variety | ≥ 8 heads, ≥ 5 skin tones, 3 builds, ≥ 2 headgear and gear variants per team; stable; the face survives halftime | 20 distinct faces in a match roster, melanin 0.0-0.92 (≥ 5 tones), 3 builds, men and women; helmet / cap / balaclava; 2-4 pouch layouts, radio side, admin pouch, holster (tests) | yes |
| offline | the sheet with `COLD_SECTOR_OFFLINE=1` reads as human | procedural faces (no downloaded files) | yes |
| teams at 40 m in greyscale | reported | mean luminance of soldier pixels in the 40 m shot: attack 0.289, defence 0.280 (difference 0.009; the mannequins: 0.018) | reported, see below |
| hit boxes | areas within ±10 %, reported, then asked | capsules fitted to the soldier, as you chose (table below); head centres within 1 cm (20 of 20, worst 0.8 cm); every capsule axis inside the body | as decided |
| AI balance after the new hit boxes | the A metrics and a head-to-head subset re-run | being re-run | pending |

## The soldiers

* **Bodies** (`characters/human.py`, `clothing.py`, `gear.py`): signed distance fields along the game
  skeleton's bind pose, meshed with surface nets and simplified with quadric decimation to the LOD
  budgets (`characters/build.py`, `mesher.py`, `decimate.py`). Combat shirt (full or rolled sleeves),
  trousers, boots, gloves or bare hands, plate carrier with pouches, radio, admin pouch and holster
  per layout, belt and knee pads; helmet (cover, ear protection, goggles), cap or balaclava.
* **Faces**: MakeHuman / MPFB2 (CC0) heads when the files are fetched
  (`python tools/download_assets.py --only characters`; `characters/makehuman.py`): the base mesh
  with ethnicity, build and face-proportion targets per bot, placed on the game skeleton. Without
  them, or with `COLD_SECTOR_OFFLINE=1`, a procedural head. Hair, beards and brows are paint plus,
  for hair and full beards, a layer grown from the skin.
* **Materials** (`render/shaders/character.frag`): one draw call per soldier per pass; the colours
  are a per-team palette table, the shading per slot kind (skin with wrap lighting and two specular
  lobes, fabric sheen, hard surfaces, eyes, hair, lenses), a small shared detail texture.
* **Animation** (`gameplay/body.py`, `data/character_anims.json`): procedural gait, crouch, aim,
  lean, two-bone arm IK onto the weapon, clips for the events above, an additive hit flinch.
  Ragdoll (`gameplay/ragdoll.py`): 11 capsules with cone-twist and hinge limits, from the pose at
  death; it settles and freezes within a few seconds.
* **Build cost**: about 16 s per soldier and kit, once; 20 for a match are built in parallel at the
  first start and cached in `assets/cache/characters/`.

## Hit boxes

Capsules on the bones (`gameplay/hitboxes.py` `CAPSULES`), the same for every appearance, fitted to
the visible soldier (`tools/fit_hitboxes.py`). Areas in cm² (5 mm grid, first hit) against the
baseline; "visible, no hit box" is the soldier's silhouette with no capsule behind it, "hit box,
nothing visible" the capsule outside the silhouette.

| view | head | chest | stomach | arm | leg | total | visible, no hit box | hit box, nothing visible |
|---|---|---|---|---|---|---|---|---|
| stand front | 384 (+0 %) | 861 (-9 %) | 1335 (+1 %) | 888 (-17 %) | 2346 (-9 %) | 5813 (-8 %) | 410 | 305 |
| stand side | 381 (+1 %) | 510 (+16 %) | 596 (-15 %) | 834 (-25 %) | 1387 (-4 %) | 3707 (-9 %) | 758 | 218 |
| crouch front | 385 (+1 %) | 839 (-17 %) | 1157 (+47 %) | 925 (-19 %) | 1200 (-28 %) | 4506 (-9 %) | 233 | 328 |
| crouch side | 381 (+1 %) | 511 (+14 %) | 554 (-19 %) | 836 (-24 %) | 1336 (+2 %) | 3618 (-8 %) | 474 | 269 |

With the old boxes on the new soldier the mismatch was 288 + 696 cm² from the front and 524 + 385
from the side. No capsule set keeps every group within ±10 % of the mannequin's areas: the
soldiers hold the rifle across the chest, and the arms cover the chest and stomach.

Shots test the pose of the last physics step, as before (Panda's Bullet moves kinematic bodies
only inside the step; at most one 15.6 ms tick behind). Kept as you chose.

## Teams at 40 m

The 40 m shot is backlit: the soldiers' fronts are in shade and, at about 8 pixels wide, their
edges blend into the wall behind them, so the uniforms' albedo difference (attack shirt about 0.54,
defence about 0.29 in sRGB) mostly disappears in the measured luminance. By hue the teams stay
apart (tan and slate). Changing the team colours is a design call; not done.

## Known issues

* Hair lines and brows are limited by the head's vertex spacing (about 1 cm at LOD0): soft at 2 m.
* The first start builds every soldier once (about 1.5-2 minutes for a full match on 4 cores).
* The team colours separate by hue more than by value at 40 m (above).

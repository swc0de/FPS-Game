"""Player weapon controller: input -> weapon state -> ballistics/effects/viewmodel.

Runs inside the fixed 64 Hz tick for anything that affects gameplay (fire
timing, hit scans, grenade throws) and once per rendered frame for
presentation (viewmodel, recoil view offset, FOV zoom, crosshair).
"""
from __future__ import annotations

import math
import random

from panda3d.core import Point3, Vec3

from engine.physics import MASK_BULLETS
from gameplay.damage import DamageInfo
from weapons.defs import database
from weapons.grenades import Grenade, throw_velocity
from weapons.inventory import Inventory
from weapons.viewmodel import Spring, Viewmodel
from weapons.weapon import AimContext, angles_to_dir, apply_offset

SLOT_KEYS = (("slot1", "primary"), ("slot2", "secondary"), ("slot3", "melee"), ("slot4", "grenade"))


class PlayerWeapons:
    def __init__(self, game, sandbox: bool = True):
        self.game = game
        self.player = game.player
        self.db = database()
        self.rng = random.Random()
        self.inv = Inventory(self.db, infinite_reserve=False, rng=self.rng)
        self.vm = Viewmodel(game)
        game.effects.attach_viewmodel(self.vm.root)
        self.punch = Spring(140.0)          # visual camera kick (pitch, yaw, roll)
        self.ads_held = False
        self.zoom = 1.0
        self.inspecting = False
        self.melee_next = 0.0
        self.melee_pending: tuple[float, bool] | None = None
        self.grenade_phase = None           # None | "priming" | "thrown"
        self.grenade_lob = False
        self.grenade_timer = 0.0
        self.switch_lock = 0.0
        self.force_hide_vm = False         # camera-shot mode hides the hands
        self.sandbox = sandbox
        self.now = 0.0
        self.give_loadout(["r7", "p9", "knife"], {"frag": 1, "flash": 2, "smoke": 1})

    # ------------------------------------------------------------ loadout
    def give_loadout(self, weapons, grenades) -> None:
        for key in weapons:
            self.inv.give_weapon(key)
        for key, n in grenades.items():
            self.inv.give_grenade(key, n)
        self.inv.slot = "melee"
        self.select(self.inv.best_slot(), force=True)

    def select(self, slot: str, force: bool = False) -> None:
        changed = self.inv.select(slot)
        if not changed and not force:
            return
        self._equip_current()

    def _equip_current(self) -> None:
        now = self.now
        self.inspecting = False
        self.grenade_phase = None
        self.melee_pending = None
        self.ads_held = False
        ws = self.inv.current()
        if ws is not None:
            ws.deploy(now)
            self.vm.equip(ws.d.model, ws.d.cls, ws.d.draw_time)
        elif self.inv.slot == "grenade" and self.inv.grenade:
            g = self.db.grenades[self.inv.grenade]
            self.vm.equip(g.model, "grenade", 0.45)
            self.switch_lock = now + 0.45
        self.game.audio.play_ui("draw")

    def current_name(self) -> str:
        ws = self.inv.current()
        if ws is not None:
            return ws.d.name
        if self.inv.slot == "grenade" and self.inv.grenade:
            return self.db.grenades[self.inv.grenade].name
        return ""

    def speed_scale(self) -> float:
        ws = self.inv.current()
        scale = ws.d.speed if ws is not None else 1.0
        if ws is not None and self.vm.ads > 0.5:
            if ws.zoom_level > 0 and ws.d.scope:
                scale *= float(ws.d.scope.get("speed", 0.6)) / max(ws.d.speed, 1e-3)
            elif ws.d.ads:
                scale *= float(ws.d.ads.get("speed", 0.8))
        return scale

    # --------------------------------------------------------------- tick
    def fixed_update(self, dt: float, now: float) -> None:
        self.now = now
        inp = self.game.input
        player = self.player
        alive = player.damageable.alive
        if not alive:
            return
        for action, slot in SLOT_KEYS:
            if inp.consume(action):
                self.select(slot)
        if inp.consume("wheel_up"):
            if self.inv.cycle(-1):
                self._equip_current()
        if inp.consume("wheel_down"):
            if self.inv.cycle(1):
                self._equip_current()
        if inp.consume("last_weapon") and self.inv.last_slot and self.inv.has(self.inv.last_slot):
            self.select(self.inv.last_slot)
        if inp.consume("drop"):
            self.drop_current()
        if inp.consume("use"):
            self.try_pickup()
        self._auto_pickup()

        ws = self.inv.current()
        if ws is not None:
            ws.update(now, dt)
            if inp.consume("reload"):
                if ws.start_reload(now):
                    self.inspecting = False
            if inp.consume("inspect") and not ws.busy(now):
                self.inspecting = True
                self.vm.play("inspect", 3.0)
            self._ads_logic(ws, inp)
            trigger = inp.is_down("fire")
            pressed = inp.consume("fire")
            if ws.d.fire_mode == "melee":
                alt = inp.consume("aim")
                self._melee(ws, now, pressed or (trigger and now >= self.melee_next), alt)
            elif ws.wants_shot(now, trigger, pressed):
                self._fire(ws, now)
            self._handle_events(ws, now)
        elif self.inv.slot == "grenade":
            self._grenade_logic(dt, now, inp)
        else:
            inp.consume("fire")

    def _ads_logic(self, ws, inp) -> None:
        aim_pressed = inp.consume("aim") if ws.d.fire_mode != "melee" else False
        if ws.d.scope:
            if aim_pressed and not ws.reloading:
                zooms = ws.d.scope.get("zooms", [0.4])
                ws.zoom_level = (ws.zoom_level + 1) % (len(zooms) + 1)
                self.game.audio.play_ui("scope")
            if ws.reloading:
                ws.zoom_level = 0
            self.ads_held = ws.zoom_level > 0
        elif ws.d.ads:
            self.ads_held = inp.is_down("aim") and not ws.reloading and not self.player.noclip
        else:
            self.ads_held = False

    def aim_context(self, ws) -> AimContext:
        c = self.player.char
        return AimContext(speed=c.horizontal_speed, max_speed=c.cfg["run_speed"] * ws.d.speed,
                          on_ground=c.on_ground, crouched=c.crouched, ads=self.vm.ads,
                          scoped=ws.zoom_level > 0 and self.vm.ads > 0.9)

    def eye(self) -> Point3:
        c = self.player.char
        return Point3(c.pos.x, c.pos.y, c.pos.z + c.eye_height)

    # --------------------------------------------------------------- fire
    def _fire(self, ws, now: float) -> None:
        game = self.game
        eye = self.eye()
        ctx = self.aim_context(ws)
        offsets = ws.shot_offsets(ctx)
        tracer = ws.is_tracer() or (ws.d.tracer_every and ws.shot_count == 0 and False)
        first_dir = None
        hits = []
        for i, (dx, dy) in enumerate(offsets):
            yaw, pitch = apply_offset(self.player.yaw, self.player.pitch, dx, dy)
            d = Vec3(*angles_to_dir(yaw, pitch))
            if first_dir is None:
                first_dir = d
            res = game.ballistics.fire(eye, d, ws.d, attacker=self.player)
            for imp in res.impacts:
                game.effects.impact(imp.pos, imp.normal, imp.surface, d, imp.exit)
                if not imp.character and i % 3 == 0:
                    game.audio.play_at(f"impact_{game.ballistics.surface(imp.surface).get('sound', 'concrete')}",
                                       imp.pos, volume=0.6)
            hits += res.damage
            if (tracer and i == 0) or (ws.d.pellets > 1 and i < 2 and self.rng.random() < 0.3):
                game.effects.tracer(self.vm.muzzle_world_point(), res.end)
        ws.on_fired(now)
        if hits:
            killed = any(h.killed for h in hits)
            game.hud.hit_marker(killed, any(h.hitgroup == "head" for h in hits))
            game.audio.play_ui("hit_head" if any(h.hitgroup == "head" for h in hits) else "hit")
        empty = ws.ammo == 0
        self.vm.on_fire(ws.d.recoil.punch, empty_after=empty)
        p = ws.d.recoil.punch * (0.45 if self.vm.ads > 0.5 else 1.0)
        self.punch.impulse(Vec3(self.rng.uniform(-0.3, 0.3) * p, 9.0 * p, self.rng.uniform(-2, 2) * p))
        muzzle_cam = self.vm.current.muzzle.getPos(self.game.camera) if self.vm.current else None
        scoped = ws.zoom_level > 0 and self.vm.ads > 0.9
        game.effects.muzzle_flash(None if scoped else muzzle_cam, self.vm.muzzle_world_point(), first_dir,
                                  ws.d.muzzle_flash)
        if ws.d.fire_mode not in ("bolt", "pump"):
            pos, side = self.vm.eject_world()
            game.effects.eject_shell(ws.d.shell, pos, side, self.player.char.vel)
        game.audio.play_shot(ws.d.sound, eye, own=True)
        game.notify_noise(eye, 1.0, 70.0)
        self.inspecting = False
        if ws.d.scope and ws.d.scope.get("unscope_after_shot"):
            self.ads_held = False

    def _handle_events(self, ws, now: float) -> None:
        for ev in ws.pop_events():
            k = ev.kind
            cls = ws.d.cls
            if k == "reload_start":
                self.inspecting = False
                if cls == "shotgun":
                    self.vm.play("shotgun_load_start", float(ws.d.raw.get("reload_start_time", 0.35)), hold=True)
                else:
                    name = {"pistol": "reload_pistol", "sniper": "reload_sniper"}.get(cls, "reload_rifle")
                    if ev.data.get("kind") == "empty" and cls in ("rifle", "smg", "pistol"):
                        name += "_empty"
                    self.vm.play(name, 1.0, driver=lambda w=ws: w.reload_progress(self.now))
                if cls != "shotgun":
                    kind = "empty" if ev.data.get("kind") == "empty" else "tactical"
                    self.game.audio.play_ui(f"reload_{ws.d.key}_{kind}")
            elif k == "shell":
                self.vm.play("shotgun_shell", float(ws.d.raw.get("reload_shell_time", 0.5)), hold=True)
                self.game.audio.play_ui("shell_insert")
            elif k == "reload_done":
                if cls == "shotgun":
                    self.vm.stop_track()
                    self.vm.play_part("pump_cycle", 0.55)
            elif k == "cycle":
                if cls == "sniper":
                    self.vm.play_part("bolt_cycle", ws.d.fire_interval * 0.9)
                    self.game.taskMgr.doMethodLater(ws.d.fire_interval * 0.45, self._eject_later,
                                                    "eject", extraArgs=[ws.d.shell])
                    self.game.audio.play_ui("bolt")
                else:
                    self.vm.play_part("pump_cycle", min(ws.d.fire_interval * 0.8, 0.6))
                    self.game.taskMgr.doMethodLater(0.22, self._eject_later, "eject", extraArgs=[ws.d.shell])
                    self.game.audio.play_ui("pump")
            elif k == "dry":
                self.game.audio.play_ui("dry_fire")
            elif k == "draw":
                pass

    def _eject_later(self, kind):
        pos, side = self.vm.eject_world()
        self.game.effects.eject_shell(kind, pos, side, self.player.char.vel)
        return None

    # --------------------------------------------------------------- melee
    def _melee(self, ws, now: float, light: bool, heavy: bool) -> None:
        if self.melee_pending is not None and now >= self.melee_pending[0]:
            self._melee_hit(ws, self.melee_pending[1])
            self.melee_pending = None
        if now < self.melee_next or now < ws.draw_end or not (light or heavy):
            return
        rpm = float(ws.d.raw.get("heavy_rpm", 60)) if heavy else ws.d.rpm
        self.melee_next = now + 60.0 / rpm
        self.melee_pending = (now + (0.25 if heavy else 0.1), heavy)
        self.vm.play("knife_stab" if heavy else "knife_slash", 60.0 / rpm * 0.9)
        self.game.audio.play_ui("knife_swing", 0.5)

    def _melee_hit(self, ws, heavy: bool) -> None:
        eye = self.eye()
        d = Vec3(*angles_to_dir(self.player.yaw, self.player.pitch))
        rng = float(ws.d.raw.get("melee_range", 1.6))
        res = self.game.physics.world.rayTestClosest(eye, eye + d * rng, MASK_BULLETS)
        if not res.hasHit():
            return
        node = res.getNode()
        pos = Point3(res.getHitPos())
        owner = node.getPythonTag("owner")
        if owner is not None and owner is not self.player and hasattr(owner, "damageable"):
            dmg = float(ws.d.raw.get("heavy_damage", 65)) if heavy else ws.d.damage
            fwd = owner.forward() if hasattr(owner, "forward") else None
            if fwd is not None and fwd.dot(d) > 0.5:
                dmg *= float(ws.d.raw.get("backstab_multiplier", 2.8))
            hg = node.getTag("hitgroup") or "chest"
            info = DamageInfo(dmg, ws.d.armor_penetration, hg, "melee", self.player, ws.d.key, tuple(pos), tuple(d))
            r = owner.damageable.take_damage(info)
            if r is not None:
                if hasattr(owner, "on_hit"):
                    owner.on_hit(r, pos, d)
                self.game.hud.hit_marker(r.killed, False)
            self.game.effects.impact(pos, Vec3(res.getHitNormal()), node.getTag("surface") or "flesh", d)
            self.game.audio.play_at("knife_hit_body", pos)
        else:
            from engine.physics import surface_of
            self.game.effects.impact(pos, Vec3(res.getHitNormal()), surface_of(node), d)
            self.game.audio.play_at("knife_hit_wall", pos)

    # ------------------------------------------------------------ grenades
    def _grenade_logic(self, dt: float, now: float, inp) -> None:
        fire_down = inp.is_down("fire")
        aim_down = inp.is_down("aim")
        fp = inp.consume("fire")
        ap = inp.consume("aim")
        if now < self.switch_lock:
            return
        if self.grenade_phase is None and (fp or ap or fire_down or aim_down):
            self.grenade_phase = "priming"
            self.grenade_timer = 0.0
            self.vm.play("grenade_prime", 0.35, hold=True)
            self.game.audio.play_ui("pin")
        elif self.grenade_phase == "priming":
            self.grenade_timer += dt
            self.grenade_lob = aim_down and not fire_down
            if self.grenade_timer > 0.3 and not fire_down and not aim_down:
                self._throw()
        elif self.grenade_phase == "thrown":
            self.grenade_timer += dt
            if self.grenade_timer > 0.55:
                self.grenade_phase = None
                if self.inv.has("grenade"):
                    self.inv.select("grenade")
                    if self.inv.grenade not in self.inv.grenades:
                        self.inv.grenade = None
                        self.inv.select("grenade")
                    self._equip_current()
                else:
                    self.inv.slot = "melee"
                    self.select(self.inv.best_slot(), force=True)

    def _throw(self) -> None:
        key = self.inv.use_grenade()
        self.grenade_phase = "thrown"
        self.grenade_timer = 0.0
        if key is None:
            return
        g = self.db.grenades[key]
        speed = g.lob_speed if self.grenade_lob else g.throw_speed
        vel = throw_velocity(self.player.yaw, self.player.pitch, speed, self.player.char.vel)
        fwd = Vec3(*angles_to_dir(self.player.yaw, self.player.pitch))
        h = math.radians(self.player.yaw)
        right = Vec3(math.cos(h), math.sin(h), 0)
        start = self.eye() + fwd * 0.25 + right * 0.12 - Vec3(0, 0, 0.08)
        self.game.spawn_grenade(Grenade(self.game, g, start, vel, self.player))
        self.vm.play("grenade_throw", 0.5, hold=True)
        self.game.audio.play_ui("throw")

    # ------------------------------------------------------------- pickups
    def drop_current(self) -> None:
        ws = self.inv.drop_current()
        if ws is None:
            return
        ws.cancel_reload()
        ws.zoom_level = 0
        fwd = Vec3(*angles_to_dir(self.player.yaw, self.player.pitch))
        self.game.pickups.spawn("weapon", ws, self.eye() + fwd * 0.5, self.player.yaw,
                                vel=fwd * 4.0 + Vec3(0, 0, 1.5) + self.player.char.vel)
        self._equip_current()

    def try_pickup(self) -> bool:
        fwd = Vec3(*angles_to_dir(self.player.yaw, self.player.pitch))
        p = self.game.pickups.look_at(self.eye(), fwd)
        if p is None:
            return False
        return self._take(p)

    def _auto_pickup(self) -> None:
        for p in self.game.pickups.near_feet(self.player.char.pos):
            if p.kind == "weapon":
                ws = p.item if not isinstance(p.item, str) else None
                slot = ws.d.slot if ws else self.db.weapons[p.item].slot
                if not self.inv.has(slot):
                    self._take(p)

    def _take(self, p) -> bool:
        if p.kind == "weapon":
            item = p.item
            if isinstance(item, str):
                item = self.inv.make(item)
            slot = item.d.slot
            old = self.inv.weapons.get(slot)
            if old is not None:
                old.cancel_reload()
                fwd = Vec3(*angles_to_dir(self.player.yaw, self.player.pitch))
                self.game.pickups.spawn("weapon", old, self.eye() + fwd * 0.4, self.player.yaw,
                                        vel=fwd * 2.5 + Vec3(0, 0, 1.0))
            self.inv.give_weapon(item)
            p.taken()
            self.inv.slot = "melee"
            self.select(slot, force=True)
            self.game.hud.flash_msg(f"picked up {item.d.name}")
        elif p.kind == "ammo":
            self.inv.refill_ammo()
            self.game.hud.flash_msg("ammo refilled")
        elif p.kind == "armor":
            self.player.damageable.armor = 100
            self.player.damageable.helmet = True
            self.game.hud.flash_msg("armour + helmet")
        elif p.kind == "grenades":
            self.inv.refill_grenades()
            self.game.hud.flash_msg("grenades refilled")
        elif p.kind == "grenade":
            if not self.inv.give_grenade(p.item):
                return False
            p.taken()
        self.game.audio.play_ui("pickup")
        return True

    # -------------------------------------------------------------- frame
    def pre_frame(self, dt: float) -> None:
        """Camera offsets (recoil view follow + punch + shake), before the player camera update."""
        ws = self.inv.current()
        vx, vy = ws.view_offset() if ws is not None else (0.0, 0.0)
        pk = self.punch.update(dt)
        shake = self.game.effects.shake
        sx = sy = 0.0
        if shake > 0:
            t = self.game.loop.time
            sx = (math.sin(t * 61.0) + math.sin(t * 37.0)) * shake * 0.5
            sy = (math.sin(t * 53.0) + math.cos(t * 29.0)) * shake * 0.5
        self.player.view_offset = (-vx + pk.x * 0.1 + sx, vy + pk.y * 0.1 + sy, pk.z * 0.1)

    def frame_update(self, dt: float) -> None:
        ws = self.inv.current()
        ads_target = 1.0 if self.ads_held else 0.0
        ads_time = 0.2
        zoom = 1.0
        scoped = False
        if ws is not None:
            if ws.d.scope:
                ads_time = float(ws.d.scope.get("time", 0.12))
                if ws.zoom_level > 0:
                    zoom = ws.d.scope["zooms"][ws.zoom_level - 1]
            elif ws.d.ads:
                ads_time = float(ws.d.ads.get("time", 0.2))
                zoom = float(ws.d.ads.get("zoom", 0.8))
        blend = self.vm.ads
        cur_zoom = 1.0 + (zoom - 1.0) * blend
        if ws is not None and ws.d.scope and ws.zoom_level > 0:
            scoped = blend > 0.85
            cur_zoom = zoom if scoped else 1.0 + (zoom - 1.0) * blend
        self.game.set_zoom(cur_zoom)
        self.vm.set_hidden(scoped or not self.player.damageable.alive or self.force_hide_vm)
        self.game.hud.set_scope(scoped)
        p = self.player
        self.vm.update(dt, {
            "ads": ads_target, "ads_time": ads_time, "mouse": p.last_mouse,
            "bob_phase": p.bob_phase, "bob_weight": p.bob_weight,
            "crouch": 1.0 if p.char.crouched else 0.0, "land": p.land_offset,
        })
        if ws is not None and ws.d.is_gun:
            spread = ws.inaccuracy(self.aim_context(ws))
            self.game.hud.set_spread(spread, self.game.current_vfov())
        else:
            self.game.hud.set_spread(0.0, self.game.current_vfov())

    def ammo_text(self) -> tuple[str, str]:
        ws = self.inv.current()
        if ws is not None and ws.d.magazine > 0:
            return f"{ws.ammo}", f"/ {ws.reserve}"
        if self.inv.slot == "grenade" and self.inv.grenade:
            return f"{self.inv.grenades.get(self.inv.grenade, 0)}", ""
        return "", ""

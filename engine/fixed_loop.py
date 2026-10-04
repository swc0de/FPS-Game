"""Fixed-timestep simulation driver.

Rendering runs as fast as the display allows while gameplay advances in
fixed ticks (64 Hz by default, like competitive shooters).  The renderer
interpolates between the last two simulation states using ``alpha`` so
motion stays smooth at any frame rate.

    accumulator += frame_dt
    while accumulator >= tick_dt:
        fixed_update(tick_dt)
        accumulator -= tick_dt
    alpha = accumulator / tick_dt   # 0..1 blend factor for rendering
"""
from __future__ import annotations

from typing import Callable


class FixedTimestep:
    def __init__(self, tick_rate: float = 64.0, max_ticks_per_frame: int = 8):
        self.tick_rate = tick_rate
        self.dt = 1.0 / tick_rate
        self.max_ticks_per_frame = max_ticks_per_frame
        self.accumulator = 0.0
        self.tick_count = 0
        self.time = 0.0  # simulation time in seconds

    def advance(self, frame_dt: float, fixed_update: Callable[[float], None]) -> float:
        """Run as many fixed ticks as needed; return the interpolation alpha."""
        # Clamp huge hitches (window drag, breakpoints) so we don't spiral.
        frame_dt = min(max(frame_dt, 0.0), self.dt * self.max_ticks_per_frame)
        self.accumulator += frame_dt
        ticks = 0
        while self.accumulator >= self.dt and ticks < self.max_ticks_per_frame:
            fixed_update(self.dt)
            self.accumulator -= self.dt
            self.tick_count += 1
            self.time += self.dt
            ticks += 1
        if ticks == self.max_ticks_per_frame:
            self.accumulator = min(self.accumulator, self.dt)
        return self.accumulator / self.dt

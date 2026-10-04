#version 330
// bloom_down.frag - one step of the bloom downsample chain
// (Jimenez, "Next Generation Post Processing in Call of Duty: AW", 2014).
//
// 13 bilinear taps cover a 6x6 texel footprint as five overlapping 2x2
// boxes (centre box weighted 0.5, four corner boxes 0.125 each), which
// avoids the shimmering of a plain 2x2 box filter on moving highlights.
//
// The FIRST pass (full res -> 1/2) also:
//   * applies the current exposure and a soft-knee threshold, so only
//     things brighter than the display white bloom (muzzle flashes, lamps,
//     the sun's reflection, explosions),
//   * weights each box by 1 / (1 + luma) ("Karis average"), which stops
//     single very bright pixels (fireflies) from flickering into big blobs.

#include "common.glsl"
#include "exposure.glsl"

uniform sampler2D u_src;
uniform vec2 u_srcTexel;
uniform vec4 u_bloomParams;     // x: 1 = first pass, y: threshold, z: knee, w: clamp

in vec2 v_uv;
layout(location = 0) out vec4 o_color;

vec3 tap(vec2 o) { return textureLod(u_src, v_uv + o * u_srcTexel, 0.0).rgb; }

float karis(vec3 c) { return 1.0 / (1.0 + luminance(c)); }

void main() {
    vec3 a = tap(vec2(-2.0, 2.0)), b = tap(vec2(0.0, 2.0)), c = tap(vec2(2.0, 2.0));
    vec3 d = tap(vec2(-2.0, 0.0)), e = tap(vec2(0.0, 0.0)), f = tap(vec2(2.0, 0.0));
    vec3 g = tap(vec2(-2.0, -2.0)), h = tap(vec2(0.0, -2.0)), i = tap(vec2(2.0, -2.0));
    vec3 j = tap(vec2(-1.0, 1.0)), k = tap(vec2(1.0, 1.0));
    vec3 l = tap(vec2(-1.0, -1.0)), m = tap(vec2(1.0, -1.0));

    vec3 result;
    if (u_bloomParams.x > 0.5) {
        float exposure = sceneExposure();
        vec3 b0 = (j + k + l + m) * 0.25;
        vec3 b1 = (a + b + d + e) * 0.25;
        vec3 b2 = (b + c + e + f) * 0.25;
        vec3 b3 = (d + e + g + h) * 0.25;
        vec3 b4 = (e + f + h + i) * 0.25;
        float w0 = 0.5 * karis(b0), w1 = 0.125 * karis(b1), w2 = 0.125 * karis(b2);
        float w3 = 0.125 * karis(b3), w4 = 0.125 * karis(b4);
        result = (b0 * w0 + b1 * w1 + b2 * w2 + b3 * w3 + b4 * w4) / (w0 + w1 + w2 + w3 + w4);
        result = min(result * exposure, vec3(u_bloomParams.w));
        // soft-knee threshold on the brightest channel
        float br = max(result.r, max(result.g, result.b));
        float knee = max(u_bloomParams.z, 1e-4);
        float soft = clamp(br - u_bloomParams.y + knee, 0.0, 2.0 * knee);
        soft = soft * soft / (4.0 * knee);
        float contrib = max(soft, br - u_bloomParams.y) / max(br, 1e-4);
        result *= contrib;
    } else {
        result = e * 0.125;
        result += (a + c + g + i) * 0.03125;
        result += (b + d + f + h) * 0.0625;
        result += (j + k + l + m) * 0.125;
    }
    o_color = vec4(max(result, vec3(0.0)), 1.0);
}

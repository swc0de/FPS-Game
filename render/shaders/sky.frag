#version 330
// sky.frag - HDRI background + analytic sun disc.
//
// The sky is stored as an equirectangular HDR image (Z-up longitude/latitude).
// The HDRI's own sun is clamped during IBL processing; the visible sun disc is
// drawn analytically here with a soft limb so it blooms convincingly and
// stays aligned with the shadow-casting directional light.

#include "common.glsl"
#include "fog.glsl"

uniform sampler2D u_skyTex;
uniform vec4 u_skyParams;  // x: intensity, y: sun disc angular radius (rad), z: sun disc intensity, w: horizon fog blend
uniform vec3 u_sunDir;
uniform vec3 u_sunColor;

in vec3 v_dir;
layout(location = 0) out vec4 o_color;

vec2 equirectUV(vec3 d) {
    float u = atan(d.y, d.x) * (0.5 / PI) + 0.5;
    float v = asin(clamp(d.z, -1.0, 1.0)) / PI + 0.5;
    return vec2(u, v);
}

void main() {
    vec3 d = normalize(v_dir);
    vec3 col = textureLod(u_skyTex, equirectUV(d), 0.0).rgb * u_skyParams.x;
    float c = dot(d, u_sunDir);
    float cosR = cos(u_skyParams.y);
    if (c > cosR - 0.002) {
        // limb darkening for a less "flat" disc
        float t = saturate((c - cosR) / (1.0 - cosR));
        float limb = 0.4 + 0.6 * sqrt(t);
        float edge = smoothstep(cosR - 0.002, cosR + 0.0005, c);
        col += u_sunColor * u_skyParams.z * limb * edge;
    }
    // blend the lower sky toward the fog colour so distant geometry and the
    // horizon meet without a visible seam
    float horizon = saturate(1.0 - abs(d.z) * 6.0) * u_skyParams.w;
    col = mix(col, fogInscatter(d, u_sunDir), horizon * u_fogParams.w);
    o_color = vec4(col, 1.0);
}

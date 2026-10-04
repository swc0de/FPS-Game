#version 330
// ao_blur.frag - separable depth-aware (bilateral) blur for GTAO.
//
// A 9-tap Gaussian along u_dir whose weights also fall off with the
// relative depth difference to the centre pixel, so occlusion does not
// bleed across silhouettes (e.g. from a wall onto the sky behind a crate).
// Run twice (horizontal, vertical). The linear depth travels along in .g
// for the next pass and for the depth-aware upsampling in the main shader.

uniform sampler2D u_src;        // r: AO, g: linear depth
uniform vec2 u_dir;             // texel step (1/w, 0) or (0, 1/h)
uniform float u_sharpness;      // depth sensitivity

in vec2 v_uv;
layout(location = 0) out vec4 o_ao;

void main() {
    vec2 c = textureLod(u_src, v_uv, 0.0).rg;
    if (c.g > 9000.0) { o_ao = vec4(1.0, c.g, 0.0, 1.0); return; }
    float sum = c.r;
    float wsum = 1.0;
    for (int i = 1; i <= 4; ++i) {
        float g = exp(-float(i * i) / 8.0);
        for (int s = -1; s <= 1; s += 2) {
            vec2 t = textureLod(u_src, v_uv + u_dir * float(i * s), 0.0).rg;
            float dz = abs(t.g - c.g) / max(c.g, 1e-3);
            float w = g * exp(-dz * u_sharpness);
            sum += t.r * w;
            wsum += w;
        }
    }
    o_ao = vec4(sum / wsum, c.g, 0.0, 1.0);
}

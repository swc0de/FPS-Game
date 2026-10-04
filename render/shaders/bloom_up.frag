#version 330
// bloom_up.frag - one step of the bloom upsample chain.
//
// The smaller (blurrier) level is upsampled with a 3x3 tent filter and
// blended with this level's downsample: result = mix(high, tent(low),
// scatter). Repeating this from the smallest level up sums Gaussian-like
// blurs of many radii, which gives the long, natural falloff of light
// scattering in a lens - at the cost of only a few taps per pixel.

uniform sampler2D u_low;        // smaller level (already upsampled)
uniform sampler2D u_high;       // this level's downsample
uniform vec2 u_lowTexel;
uniform float u_scatter;

in vec2 v_uv;
layout(location = 0) out vec4 o_color;

void main() {
    vec2 t = u_lowTexel;
    vec3 s = textureLod(u_low, v_uv, 0.0).rgb * 4.0;
    s += textureLod(u_low, v_uv + vec2(-t.x, 0.0), 0.0).rgb * 2.0;
    s += textureLod(u_low, v_uv + vec2(t.x, 0.0), 0.0).rgb * 2.0;
    s += textureLod(u_low, v_uv + vec2(0.0, -t.y), 0.0).rgb * 2.0;
    s += textureLod(u_low, v_uv + vec2(0.0, t.y), 0.0).rgb * 2.0;
    s += textureLod(u_low, v_uv + vec2(-t.x, -t.y), 0.0).rgb;
    s += textureLod(u_low, v_uv + vec2(t.x, -t.y), 0.0).rgb;
    s += textureLod(u_low, v_uv + vec2(-t.x, t.y), 0.0).rgb;
    s += textureLod(u_low, v_uv + vec2(t.x, t.y), 0.0).rgb;
    vec3 high = textureLod(u_high, v_uv, 0.0).rgb;
    o_color = vec4(mix(high, s / 16.0, u_scatter), 1.0);
}

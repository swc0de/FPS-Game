#version 330
// reduce.frag - average 4x4 texels into one (luminance pyramid 64->16->4->1).

uniform sampler2D u_src;

layout(location = 0) out vec4 o_color;

void main() {
    ivec2 base = ivec2(gl_FragCoord.xy) * 4;
    ivec2 lim = textureSize(u_src, 0) - 1;
    vec4 sum = vec4(0.0);
    for (int y = 0; y < 4; ++y)
        for (int x = 0; x < 4; ++x)
            sum += texelFetch(u_src, min(base + ivec2(x, y), lim), 0);
    o_color = sum / 16.0;
}

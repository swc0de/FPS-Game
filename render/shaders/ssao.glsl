// ssao.glsl - reading the blurred GTAO result in the forward pass.
//
// The AO target may be half resolution. A plain bilinear fetch would smear
// occlusion across depth discontinuities (dark halos around a gun barrel
// against the sky), so the four nearest AO texels are weighted by bilinear
// weight * depth similarity to this fragment ("joint bilateral upsampling").
// AO only darkens *ambient* light (sky / IBL); direct sun is handled by
// shadow maps and must not be dimmed by a screen-space guess.

uniform vec2 u_depthRange;          // near, far of the main camera

#ifdef SSAO
uniform sampler2D u_ssao;           // r: AO, g: linear depth
uniform vec4 u_ssaoParams;          // xy: 1 / scene size (px), z: strength (0 = off), w: share applied to local lights

float fragmentLinearDepth() {
    float n = u_depthRange.x;
    float f = u_depthRange.y;
    return 2.0 * n * f / (f + n - (gl_FragCoord.z * 2.0 - 1.0) * (f - n));
}

float screenSpaceAO() {
    if (u_ssaoParams.z <= 0.0) return 1.0;
    float z = fragmentLinearDepth();
    vec2 aoSize = vec2(textureSize(u_ssao, 0));
    vec2 p = gl_FragCoord.xy * u_ssaoParams.xy * aoSize - 0.5;
    vec2 fr = fract(p);
    ivec2 i0 = ivec2(floor(p));
    ivec2 lim = ivec2(aoSize) - 1;
    float sum = 0.0;
    float wsum = 0.0;
    float plain = 0.0;
    for (int k = 0; k < 4; ++k) {
        ivec2 o = ivec2(k & 1, k >> 1);
        vec2 s = texelFetch(u_ssao, clamp(i0 + o, ivec2(0), lim), 0).rg;
        float wb = (o.x == 1 ? fr.x : 1.0 - fr.x) * (o.y == 1 ? fr.y : 1.0 - fr.y);
        float wd = exp(-abs(s.g - z) / max(z * 0.04, 0.02));
        sum += s.r * wb * wd;
        wsum += wb * wd;
        plain += s.r * wb;
    }
    float ao = wsum > 1e-4 ? sum / wsum : plain;
    return mix(1.0, ao, u_ssaoParams.z);
}
#else
float screenSpaceAO() { return 1.0; }
#endif

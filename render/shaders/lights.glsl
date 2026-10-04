// lights.glsl - punctual local lights (point & spot), forward-shaded.
//
// Lights are packed into a float texture, 4 texels per light:
//   t0: position.xyz, range
//   t1: colour * intensity, type (0 = point, 1 = spot)
//   t2: spot direction.xyz, cos(outer angle)
//   t3: cos(inner angle), shadow tile (-1 = none), shadow near plane, normal offset scale
// Only the lights relevant to the current view are uploaded each frame.
//
// Falloff is physically based inverse-square, windowed to reach exactly zero at
// the light's range (Karis 2013):  (saturate(1 - (d/r)^4))^2 / (d^2 + 1)

uniform sampler2D u_lightData;
uniform int u_numLights;

float distanceAttenuation(float d, float range) {
    float r = d / range;
    float window = saturate(1.0 - r * r * r * r);
    return (window * window) / (d * d + 1.0);
}

vec3 shadeLocalLights(Surface s, vec2 pixel) {
    vec3 sum = vec3(0.0);
    float rotation = interleavedGradientNoise(pixel + 17.0) * 6.2831853;
    for (int i = 0; i < u_numLights; ++i) {
        vec4 t0 = texelFetch(u_lightData, ivec2(0, i), 0);
        vec3 toLight = t0.xyz - s.P;
        float d2 = dot(toLight, toLight);
        if (d2 > t0.w * t0.w) continue;
        float d = sqrt(d2);
        vec3 L = toLight / max(d, 1e-4);
        if (dot(s.N, L) <= 0.0) continue;
        vec4 t1 = texelFetch(u_lightData, ivec2(1, i), 0);
        vec4 t3 = texelFetch(u_lightData, ivec2(3, i), 0);
        float atten = distanceAttenuation(d, t0.w);
        if (t1.w > 0.5) {
            vec4 t2 = texelFetch(u_lightData, ivec2(2, i), 0);
            float cd = dot(-L, t2.xyz);
            atten *= smoothstep(t2.w, t3.x, cd);
        }
        if (atten <= 1e-5) continue;
#ifdef LOCAL_SHADOWS
        if (t3.y >= 0.0) {
            int tile = int(t3.y + 0.5);
            if (t1.w < 0.5) tile += cubeFace(-L);
            atten *= localShadow(tile, s.P, s.N, L, d, t3.z, t3.w, rotation);
        }
#endif
        sum += shadeDirect(s, L, t1.rgb * atten);
    }
    return sum;
}

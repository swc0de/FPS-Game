// brdf.glsl - physically based shading model (metal/roughness workflow).
//
// We use the Cook-Torrance microfacet specular BRDF
//     f_spec = D(h) * V(l, v) * F(v, h)
// with
//   D : GGX / Trowbridge-Reitz normal distribution (long specular tails that
//       match real materials),
//   V : height-correlated Smith visibility term (includes the 1/(4 NoL NoV)
//       denominator, Heitz 2014),
//   F : Schlick's Fresnel approximation,
// plus a Lambertian diffuse lobe. "alpha" is the perceptual roughness
// squared (Burley's remapping) so roughness textures behave linearly.
//
// Metals have no diffuse and tint their specular with the base colour; dielectrics
// reflect ~4% at normal incidence (F0 = 0.04).

float D_GGX(float NoH, float alpha) {
    float a2 = alpha * alpha;
    float f = (NoH * a2 - NoH) * NoH + 1.0;
    return a2 / (PI * f * f + 1e-7);
}

float V_SmithGGXCorrelated(float NoV, float NoL, float alpha) {
    float a2 = alpha * alpha;
    float ggxL = NoV * sqrt((-NoL * a2 + NoL) * NoL + a2);
    float ggxV = NoL * sqrt((-NoV * a2 + NoV) * NoV + a2);
    return 0.5 / (ggxV + ggxL + 1e-5);
}

vec3 F_Schlick(vec3 f0, float VoH) {
    float f = pow(1.0 - VoH, 5.0);
    return f0 + (vec3(1.0) - f0) * f;
}

struct Surface {
    vec3 P;          // world position
    vec3 N;          // shading normal (world)
    vec3 V;          // direction to the eye
    float NoV;
    vec3 diffuse;    // diffuse colour  = baseColor * (1 - metallic)
    vec3 f0;         // specular colour at normal incidence
    float roughness; // perceptual roughness
    float alpha;     // roughness^2 (after specular anti-aliasing)
    float ao;
};

// Radiance reflected toward the eye from one light arriving from direction L
// with radiance 'radiance' (already attenuated and shadowed).
vec3 shadeDirect(Surface s, vec3 L, vec3 radiance) {
    float NoL = saturate(dot(s.N, L));
    if (NoL <= 0.0) return vec3(0.0);
    vec3 H = normalize(L + s.V);
    float NoH = saturate(dot(s.N, H));
    float VoH = saturate(dot(s.V, H));
    float D = D_GGX(NoH, s.alpha);
    float Vis = V_SmithGGXCorrelated(s.NoV, NoL, s.alpha);
    vec3 F = F_Schlick(s.f0, VoH);
    vec3 spec = D * Vis * F;
    vec3 diff = s.diffuse * (1.0 / PI) * (vec3(1.0) - F);
    return (diff + spec) * radiance * NoL;
}

// Analytical approximation of the pre-integrated split-sum DFG term
// (Karis, "Physically Based Shading on Mobile"). Avoids a BRDF LUT texture.
vec2 envBRDFApprox(float roughness, float NoV) {
    const vec4 c0 = vec4(-1.0, -0.0275, -0.572, 0.022);
    const vec4 c1 = vec4(1.0, 0.0425, 1.04, -0.04);
    vec4 r = roughness * c0 + c1;
    float a004 = min(r.x * r.x, exp2(-9.28 * NoV)) * r.x + r.y;
    return vec2(-1.04, 1.04) * a004 + r.zw;
}

// Lagarde & de Rousiers 2014: derive specular occlusion from AO so cavities
// don't reflect the bright sky.
float specularOcclusion(float NoV, float ao, float roughness) {
    return saturate(pow(NoV + ao, exp2(-16.0 * roughness - 1.0)) - 1.0 + ao);
}

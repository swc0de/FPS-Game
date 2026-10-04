// ibl.glsl - image based lighting from the HDRI sky.
//
// DIFFUSE: the sky's irradiance is projected onto 9 spherical-harmonic
// coefficients (Ramamoorthi & Hanrahan 2001). Evaluating 9 SH terms gives
// the cosine-convolved irradiance for any normal at the cost of a few MADs.
// The coefficients already include the cosine-lobe convolution and 1/pi, so
// the result multiplies the diffuse colour directly.
//
// SPECULAR: "split sum" approximation (Karis 2013). The environment is
// pre-filtered with a GGX lobe into the mip chain of a cube map (rougher =
// blurrier = smaller mip). At runtime we fetch the mip matching the surface
// roughness along the reflection vector and scale by the pre-integrated
// BRDF term (envBRDFApprox).
//
// SKY VISIBILITY: outdoor IBL must not light interiors. A low-resolution 3D
// volume baked at load time stores, per probe, the fraction of the upper
// hemisphere that sees the sky (alpha) and the average unoccluded direction
// ("bent normal", rgb). Interiors therefore get only the light that comes
// in through doors and windows.

uniform vec3 u_sh[9];
uniform samplerCube u_envSpec;
uniform vec4 u_iblParams;     // x: diffuse intensity, y: specular intensity, z: max lod, w: unused
uniform sampler3D u_skyVis;
uniform vec3 u_skyVisMin;
uniform vec3 u_skyVisInvSize;
uniform vec3 u_ambientFloor;  // tiny constant so pitch-black corners are never fully black

vec3 shIrradiance(vec3 n) {
    // real SH basis, band 0..2 (constants folded into the coefficients)
    return max(
        u_sh[0]
        + u_sh[1] * n.y + u_sh[2] * n.z + u_sh[3] * n.x
        + u_sh[4] * (n.x * n.y) + u_sh[5] * (n.y * n.z)
        + u_sh[6] * (3.0 * n.z * n.z - 1.0)
        + u_sh[7] * (n.x * n.z) + u_sh[8] * (n.x * n.x - n.y * n.y),
        vec3(0.0));
}

// returns (bent normal xyz, visibility w)
vec4 skyVisibility(vec3 P, vec3 N) {
    // sample slightly in front of the surface so walls don't read the
    // (dark) probes behind them
    vec3 uvw = (P + N * 0.45 - u_skyVisMin) * u_skyVisInvSize;
    vec4 s = texture(u_skyVis, uvw);
    vec3 bent = s.xyz * 2.0 - 1.0;
    return vec4(bent, s.w);
}

vec3 ambientLighting(Surface s, vec3 Nvertex, float cavityAO) {
    vec4 vis = skyVisibility(s.P, Nvertex);
    float v = vis.w;
    // bend the lookup normal toward the open direction when partly occluded
    vec3 bentN = normalize(mix(s.N, normalize(vis.xyz + s.N * 0.5 + 1e-4), (1.0 - v) * 0.6));
    vec3 diffuse = s.diffuse * shIrradiance(bentN) * u_iblParams.x * v;
    diffuse += s.diffuse * u_ambientFloor;

    vec3 R = reflect(-s.V, s.N);
    // reflections that point into geometry are also occluded by the volume
    float lod = s.roughness * u_iblParams.z;
    vec3 prefiltered = textureLod(u_envSpec, R, lod).rgb;
    vec2 ab = envBRDFApprox(s.roughness, s.NoV);
    float specVis = mix(v * v, v, s.roughness);
    float so = specularOcclusion(s.NoV, cavityAO, s.roughness) * specVis;
    vec3 specular = prefiltered * (s.f0 * ab.x + ab.y) * u_iblParams.y * so;
    return diffuse * cavityAO + specular + s.f0 * u_ambientFloor * 0.25;
}

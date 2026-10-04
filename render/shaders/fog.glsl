// fog.glsl - analytic exponential height fog with sun in-scattering.
//
// Density falls off exponentially with altitude: rho(z) = a * exp(-b (z - h0)).
// Integrating along the view ray gives a closed form (Quilez), so dusty air
// pools in low areas and the horizon fades into the sky colour. A
// Henyey-Greenstein phase term brightens the fog toward the sun.

uniform vec4 u_fogParams;  // x: density, y: height falloff, z: base height, w: max opacity
uniform vec3 u_fogColor;   // ambient fog colour (derived from the sky)
uniform vec3 u_fogSun;     // sun in-scatter colour

float fogOpacity(vec3 camPos, vec3 P) {
    vec3 rd = P - camPos;
    float dist = length(rd);
    rd /= max(dist, 1e-4);
    float a = u_fogParams.x;
    float b = max(u_fogParams.y, 1e-4);
    float h0 = camPos.z - u_fogParams.z;
    float fog;
    if (abs(rd.z) > 1e-3) {
        fog = (a / b) * exp(-h0 * b) * (1.0 - exp(-dist * rd.z * b)) / rd.z;
    } else {
        fog = a * exp(-h0 * b) * dist;
    }
    return min(1.0 - exp(-max(fog, 0.0)), u_fogParams.w);
}

vec3 fogInscatter(vec3 viewDir, vec3 sunDir) {
    float g = 0.72;
    float c = dot(viewDir, sunDir);
    float hg = (1.0 - g * g) / (4.0 * PI * pow(1.0 + g * g - 2.0 * g * c, 1.5));
    return u_fogColor + u_fogSun * hg;
}

vec3 applyFog(vec3 color, vec3 camPos, vec3 P, vec3 sunDir) {
    float f = fogOpacity(camPos, P);
    vec3 viewDir = normalize(P - camPos);
    return mix(color, fogInscatter(viewDir, sunDir), f);
}

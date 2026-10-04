// exposure.glsl - camera exposure from the eye-adaptation result.
//
// u_adapt is a 1x1 target holding the adapted (time-smoothed) log2 of the
// scene's average luminance. The map's base exposure is tuned for its
// typical outdoor view, whose average log-luminance is u_aeParams.x. When
// the view gets darker (indoors) or brighter, exposure compensates by a
// fraction (strength) of the difference, clamped to an EV range, so a dark
// room is brightened but still reads as darker than the yard outside.

uniform sampler2D u_adapt;
uniform vec4 u_exposureParams;  // x: base exposure, y: adaptation strength (0 = off), z: min EV, w: max EV
uniform vec4 u_aeParams;        // x: reference log2 luminance, y: user brightness (EV), zw: unused

float sceneExposure() {
    float avgLog = texelFetch(u_adapt, ivec2(0, 0), 0).r;
    float ev = (u_aeParams.x - avgLog) * u_exposureParams.y;
    ev = clamp(ev, u_exposureParams.z, u_exposureParams.w);
    return u_exposureParams.x * exp2(ev + u_aeParams.y);
}

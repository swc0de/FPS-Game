// common.glsl - shared constants and helpers.

#ifndef PI
#define PI 3.14159265359
#endif

float saturate(float x) { return clamp(x, 0.0, 1.0); }
vec3 saturate(vec3 x) { return clamp(x, 0.0, 1.0); }

float luminance(vec3 c) { return dot(c, vec3(0.2126, 0.7152, 0.0722)); }

// Interleaved gradient noise (Jimenez 2014): a cheap per-pixel random value
// with good blue-noise-like distribution. Used to rotate PCF kernels and to
// dither, which turns banding into fine noise.
float interleavedGradientNoise(vec2 pixel) {
    return fract(52.9829189 * fract(dot(pixel, vec2(0.06711056, 0.00583715))));
}

// Vogel (golden-angle spiral) disk: evenly distributed samples for any count.
vec2 vogelDisk(int index, int count, float rotation) {
    float r = sqrt((float(index) + 0.5) / float(count));
    float theta = float(index) * 2.39996323 + rotation;
    return r * vec2(cos(theta), sin(theta));
}

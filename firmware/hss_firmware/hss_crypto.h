// hss_crypto.h - tiny SHA-256 / HMAC-SHA256 / PBKDF2-HMAC-SHA256 (single output block) for the PIN records.
// Plain C++ (no Arduino API) so it is also compiled on the PC and checked against Python hashlib
// (tests/unit/test_firmware_crypto.py). Public-domain style implementation of FIPS 180-4 / RFC 2104 / RFC 8018.
#ifndef HSS_CRYPTO_H
#define HSS_CRYPTO_H

#include <stdint.h>
#include <stddef.h>
#include <string.h>

struct HssSha256 {
  uint32_t h[8];
  uint8_t buf[64];
  uint32_t bufLen;
  uint64_t total;
};

static const uint32_t HSS_K[64] = {
  0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
  0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
  0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
  0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
  0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
  0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
  0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
  0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2};

static inline uint32_t hss_rotr(uint32_t x, uint32_t n) { return (x >> n) | (x << (32 - n)); }

static void hss_sha256_block(HssSha256* s, const uint8_t* p) {
  uint32_t w[64];
  for (int i = 0; i < 16; i++)
    w[i] = ((uint32_t)p[4 * i] << 24) | ((uint32_t)p[4 * i + 1] << 16) | ((uint32_t)p[4 * i + 2] << 8) | p[4 * i + 3];
  for (int i = 16; i < 64; i++) {
    uint32_t s0 = hss_rotr(w[i - 15], 7) ^ hss_rotr(w[i - 15], 18) ^ (w[i - 15] >> 3);
    uint32_t s1 = hss_rotr(w[i - 2], 17) ^ hss_rotr(w[i - 2], 19) ^ (w[i - 2] >> 10);
    w[i] = w[i - 16] + s0 + w[i - 7] + s1;
  }
  uint32_t a = s->h[0], b = s->h[1], c = s->h[2], d = s->h[3], e = s->h[4], f = s->h[5], g = s->h[6], h = s->h[7];
  for (int i = 0; i < 64; i++) {
    uint32_t S1 = hss_rotr(e, 6) ^ hss_rotr(e, 11) ^ hss_rotr(e, 25);
    uint32_t ch = (e & f) ^ (~e & g);
    uint32_t t1 = h + S1 + ch + HSS_K[i] + w[i];
    uint32_t S0 = hss_rotr(a, 2) ^ hss_rotr(a, 13) ^ hss_rotr(a, 22);
    uint32_t mj = (a & b) ^ (a & c) ^ (b & c);
    uint32_t t2 = S0 + mj;
    h = g; g = f; f = e; e = d + t1; d = c; c = b; b = a; a = t1 + t2;
  }
  s->h[0] += a; s->h[1] += b; s->h[2] += c; s->h[3] += d; s->h[4] += e; s->h[5] += f; s->h[6] += g; s->h[7] += h;
}

static void hss_sha256_init(HssSha256* s) {
  static const uint32_t iv[8] = {0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19};
  memcpy(s->h, iv, sizeof(iv));
  s->bufLen = 0;
  s->total = 0;
}

static void hss_sha256_update(HssSha256* s, const uint8_t* data, size_t len) {
  s->total += len;
  while (len > 0) {
    size_t take = 64 - s->bufLen;
    if (take > len) take = len;
    memcpy(s->buf + s->bufLen, data, take);
    s->bufLen += (uint32_t)take; data += take; len -= take;
    if (s->bufLen == 64) { hss_sha256_block(s, s->buf); s->bufLen = 0; }
  }
}

static void hss_sha256_final(HssSha256* s, uint8_t out[32]) {
  uint64_t bits = s->total * 8;
  uint8_t pad = 0x80;
  hss_sha256_update(s, &pad, 1);
  uint8_t zero = 0;
  while (s->bufLen != 56) hss_sha256_update(s, &zero, 1);
  uint8_t len[8];
  for (int i = 0; i < 8; i++) len[i] = (uint8_t)(bits >> (56 - 8 * i));
  hss_sha256_update(s, len, 8);
  for (int i = 0; i < 8; i++) {
    out[4 * i] = (uint8_t)(s->h[i] >> 24); out[4 * i + 1] = (uint8_t)(s->h[i] >> 16);
    out[4 * i + 2] = (uint8_t)(s->h[i] >> 8); out[4 * i + 3] = (uint8_t)s->h[i];
  }
}

static void hss_sha256(const uint8_t* data, size_t len, uint8_t out[32]) {
  HssSha256 s; hss_sha256_init(&s); hss_sha256_update(&s, data, len); hss_sha256_final(&s, out);
}

static void hss_hmac_sha256(const uint8_t* key, size_t keyLen, const uint8_t* msg, size_t msgLen, uint8_t out[32]) {
  uint8_t k[64]; memset(k, 0, sizeof(k));
  if (keyLen > 64) hss_sha256(key, keyLen, k); else memcpy(k, key, keyLen);
  uint8_t ipad[64], opad[64];
  for (int i = 0; i < 64; i++) { ipad[i] = k[i] ^ 0x36; opad[i] = k[i] ^ 0x5c; }
  uint8_t inner[32];
  HssSha256 s;
  hss_sha256_init(&s); hss_sha256_update(&s, ipad, 64); hss_sha256_update(&s, msg, msgLen); hss_sha256_final(&s, inner);
  hss_sha256_init(&s); hss_sha256_update(&s, opad, 64); hss_sha256_update(&s, inner, 32); hss_sha256_final(&s, out);
}

// PBKDF2-HMAC-SHA256, derived key length = 32 bytes (one block). salt <= 32 bytes.
static void hss_pbkdf2_sha256(const uint8_t* pw, size_t pwLen, const uint8_t* salt, size_t saltLen, uint32_t iterations, uint8_t out[32]) {
  uint8_t msg[36];
  memcpy(msg, salt, saltLen);
  msg[saltLen] = 0; msg[saltLen + 1] = 0; msg[saltLen + 2] = 0; msg[saltLen + 3] = 1;
  uint8_t u[32], t[32];
  hss_hmac_sha256(pw, pwLen, msg, saltLen + 4, u);
  memcpy(t, u, 32);
  for (uint32_t i = 1; i < iterations; i++) {
    hss_hmac_sha256(pw, pwLen, u, 32, u);
    for (int j = 0; j < 32; j++) t[j] ^= u[j];
  }
  memcpy(out, t, 32);
}

// Constant-time equality (no early exit).
static bool hss_ct_equal(const uint8_t* a, const uint8_t* b, size_t n) {
  uint8_t d = 0;
  for (size_t i = 0; i < n; i++) d |= (uint8_t)(a[i] ^ b[i]);
  return d == 0;
}

#endif  // HSS_CRYPTO_H

# Firmware image analysis — Schneider Conext `.xf0`

Read-only static analysis of the archived firmware images. **No device or gateway
was touched.** All work was done on copies in a scratch folder; the zips were
extracted there, never in the repo. No extracted binaries or scratch files are
committed — only this document.

Files examined (the `.xf0` payloads, which are the actual device images):

| Label | Source file | Image inside | Size (bytes) |
|---|---|---|---|
| XW Pro 1.11.01 BN0049 | `xw-pro/865-6848-21.01_11_01.BN0049.xf0` | (image is the `.xf0` itself) | 1,139,080 |
| XW Pro 2.04.00 BN0029 | `xw-pro/XW-Pro-...V2.04_BN0029.zip` → `865-6848-21.02_04_00.BN0029.xf0` | 1,168,072 |
| XW+ 5548 2.07.00 BN0004 | `xw-plus/conext-xw-5548-...zip` → `865-5548-01.02_07_00.BN0004.xf0` | 596,144 |

## Verdict

**All three `.xf0` images are encrypted.** They are not plaintext, not merely
compressed, and not simple XOR obfuscation. The cipher operates on an **8-byte
block in ECB (codebook) mode**, using a **fixed key shared across the XW Pro and
XW+ firmware line** (not a per-unit key). Modifying an image so the device would
accept and run it is **not feasible** from these files alone — see "Modification"
below.

## Evidence

### 1. File type and header
`file` reports `data` for all three (no recognized container). The `.xf0` is a
raw/opaque Schneider update payload with no ELF/PE/zip/gzip wrapper.

First 8 bytes:
- XW Pro 1.11.01: `5f 0d 65 f4 e8 38 61 be`
- XW Pro 2.04.00: `5f 0d 65 f4 e8 38 61 be`  ← identical to 1.11.01
- XW+ 2.07.00:    `ae be 36 fa 86 dd e1 a0`  ← different line

The two XW Pro versions share the first 8-byte block exactly while the next block
differs — consistent with a fixed 8-byte header field that is identical between
versions, under a block cipher. The XW+ differs from byte 0, consistent with the
same scheme but a different leading field.

### 2. Entropy
Overall entropy (bits/byte, max 8.0) and 4 KB-block profile:

| Image | Overall | 4 KB blocks >7.5 | blocks <6.0 |
|---|---|---|---|
| XW Pro 1.11.01 | 7.937 | 90% | 6% |
| XW Pro 2.04.00 | 7.991 | 97% | 0% |
| XW+ 2.07.00 | 7.985 | 94% | 0% |

Near-uniform entropy throughout. This alone means compressed **or** encrypted; the
block structure below distinguishes the two.

### 3. Strings
`strings -n 6` returns ~1,000–2,000 "strings", but every one is random printable
noise — no version tags, part numbers, F/W fault codes, parameter names, symbol
names, or crypto markers (`RSA`/`AES`/`-----BEGIN`/certificate text) survive. A
plaintext or lightly-compressed embedded firmware would leak at least some of
these. None are present → no plaintext regions.

### 4. Block structure — the decisive test
A single 8-byte value recurs far more than chance allows, and it is **the same
value in all three images**:

| 8-byte block | XW Pro 1.11 | XW Pro 2.04 | XW+ 2.07 |
|---|---|---|---|
| `09 ec 09 65 a0 c8 77 73` | ×8,495 | ×2,663 | ×1,944 |

Intra-file redundancy at 8-byte alignment is 25–35% of all blocks. Viewed at
16-byte alignment the hot block appears as `09ec0965a0c87773` **doubled**, which
pins the cipher block size at **8 bytes, not 16** (so not AES).

Across versions, the two XW Pro images share **16,854 distinct 8-byte block
values** (and 2,556 distinct 16-byte blocks); each XW Pro image shares ~4,000
distinct 8-byte blocks with the XW+ image.

Interpretation: a recurring constant plaintext region (erased/zero-filled flash)
encrypts to one constant ciphertext block — the signature of **ECB mode**. The
block repeats identically across different images → the **same key** is used for
the whole product line. Chained/stream modes (CBC, CTR) and any AEAD would destroy
this repetition, so those are ruled out. Compression is ruled out because LZ/gzip
output never contains thousands of identical byte-aligned blocks.

### 5. XOR-obfuscation ruled out
If the scheme were XOR against a repeating 8-byte key, XOR-ing the image by the
hot block would collapse entropy and reveal code/zeros. It does not: after XOR the
entropy stays 7.970 (from 7.991), only 2.2% of bytes become zero, and no readable
strings appear. A 16-byte-period XOR gives the same null result. So the hot block
is `encrypt(constant)`, not a raw key — i.e. a real block cipher, not XOR.

An 8-byte block in ECB is consistent with DES/3DES, Blowfish, or a proprietary
Feistel cipher; the available data does not distinguish which, and no attempt was
made to recover the key.

## Target architecture
**Cannot be determined from these files.** The code is encrypted, so no
instruction patterns or symbol strings are visible. Schneider's published
documentation describes the XW Pro / XW+ as DSP-controlled inverters (the Conext
line historically uses TI C2000-class control processors), but that is from the
product literature, **not** verifiable from these images. Architecture would only
become visible after decryption with the manufacturer key.

## Diff: 2.04.00 vs 1.11.01
- Size grew by **28,992 bytes** (1,139,080 → 1,168,072).
- Positionally (same offset, 8-byte blocks) only **0.7%** of blocks match — because
  the size change relocates everything downstream, so offset-aligned diffing is
  uninformative under ECB.
- By **content set**, the versions share **16,854 of ~92k/98k distinct 8-byte
  blocks (~17%)** and 2,556 distinct 16-byte blocks. Longest run of consecutive
  offset-aligned identical blocks is only 40 bytes.
- Reading: a substantial body of common content survives between versions (shared
  encrypted blocks), but the image was fully rebuilt/relinked, shifting layout.
  Region-level semantic diffing is not possible without decryption.

## Modification — feasibility and what blocks it
**Not feasible from the archived files.** To produce an image the device would
accept you would need, at minimum:
1. **The firmware encryption key.** The payload is fully ciphertext under a key
   held by Schneider. Without it the contents cannot be read or coherently
   altered — flipping ciphertext bytes corrupts the corresponding plaintext block
   unpredictably.
2. **Any device-side integrity/authenticity check to still pass.** The `.xf0`
   and/or the bootloader may carry a checksum or signature over the decrypted
   image; a DER/ASN.1-style signature was not observable (it would itself be inside
   the ciphertext), so its presence can't be confirmed or ruled out from here, and
   it must be assumed.

Observations on strength (for completeness, not a procedure): an 8-byte block
cipher used in **ECB with a single line-wide key** is weak by modern standards —
it leaks structure (as shown above) and shares a key across all units, so there is
no per-device binding. That is a property of the scheme; it does not by itself make
a modified image loadable, and no key-recovery or bypass was attempted here.

## Method / reproducibility
Tools: Python 3 (entropy, block-repetition, XOR test, positional diff), `strings`,
`xxd`/`hexdump`, `unzip`. `binwalk`/`7z` were not installed; the magic-signature
scan was done in Python and found only chance hits (scattered 2–4 byte matches at
densities expected from random data), i.e. **no genuine embedded archives,
filesystems, or certificates**. Scripts and extracted images live only in the
scratch folder and are not committed.

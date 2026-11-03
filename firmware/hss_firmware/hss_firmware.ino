// ============================================================
// Multi-factor access system (STM32F401 Black Pill)
// PIN keypad -> Face (host PC over USB serial) -> Fingerprint
// Protected file on SD card, audit log on SD card, OLED status
//
// HSS Stage 3 working copy. Base: firmware_secure_updated.ino (SKETCH-DERIVED,
// not proven identical to the flashed build). Changes vs base are marked HSS-S3.
// Protocol: docs/SERIAL_PROTOCOL.md (conformance-tested against hss/core/protocol.py).
// ============================================================

#include <Wire.h>
#include <SPI.h>
#include <SD.h>
#include <Adafruit_GFX.h>
#include <Adafruit_SSD1306.h>
#include <Adafruit_Fingerprint.h>

// ---- HSS-S3: build configuration ------------------------------------------------
#define HSS_FW_VERSION "0.4.0-stage6"
// 0 = PIN-only authentication (default, test mode). 1 = THREE-FACTOR: fingerprint -> face (host) -> per-user PIN
// (docs/THREE_FACTOR_DESIGN.md).
// Face/fingerprint code stays compiled in either way; with 0 it is simply not invoked
// for authentication. Build with:  --build-property "build.extra_flags=-DHSS_REQUIRE_FACTORS=1"
#ifndef HSS_REQUIRE_FACTORS
#define HSS_REQUIRE_FACTORS 0
#endif
// pins_local.h defines HSS_PIN_SLOT1 / HSS_PIN_SLOT2 (digit strings). It is NOT committed.
// Copy pins_local.example.h to pins_local.h (build.ps1 does this with random test PINs).
#include "pins_local.h"
#if HSS_REQUIRE_FACTORS
#include <EEPROM.h>
#include "hss_crypto.h"
#endif

// Set to 1 only if your installed SD library supports SD.end().
#define USE_SD_END 0

// ---------------- OLED ----------------
#define SCREEN_WIDTH 128
#define SCREEN_HEIGHT 64
#define OLED_RESET -1
#define OLED_ADDR 0x3C
Adafruit_SSD1306 display(SCREEN_WIDTH, SCREEN_HEIGHT, &Wire, OLED_RESET);

// ---------------- Fingerprint ----------------
#define fingerSerial Serial2
Adafruit_Fingerprint finger(&fingerSerial);

// ---------------- SD card ----------------
const uint8_t SD_CS = PA4;
const char* PROTECTED_FILE = "protect2.txt";
const char* AUDIT_FILE = "audit.log";

const uint16_t SD_POWERUP_DELAY_MS = 1000;
const uint16_t SD_RETRY_DELAY_MS = 500;
const unsigned long SD_REINIT_INTERVAL_MS = 10000UL;

bool sdReady = false;
bool spiConfigured = false;
unsigned long lastSdRetry = 0;

// ---------------- Keypad ----------------
const uint8_t rowPins[4] = {PB0, PB1, PB2, PB3};
const uint8_t colPins[4] = {PB4, PB5, PB6, PB7};

const char keyMap[4][4] = {
  {'1', '2', '3', 'A'},
  {'4', '5', '6', 'B'},
  {'7', '8', '9', 'C'},
  {'*', '0', '#', 'D'}
};

// ---------------- Security ----------------
// HSS-S3: two PIN slots (1 = admin test, 2 = user test). The firmware reports the slot
// number only; it never knows or reports a role (role lives in the desktop identity file).
const char* const SLOT_PINS[2] = {HSS_PIN_SLOT1, HSS_PIN_SLOT2};
uint8_t activeSlot = 0;  // 0 = none
const uint8_t MAX_PIN_LEN = 12;
const int MAX_ATTEMPTS = 3;
const unsigned long SESSION_TIMEOUT_MS = 5UL * 60UL * 1000UL;
const unsigned long ENROLL_AUTH_TIMEOUT_MS = 60UL * 1000UL;

char enteredPIN[MAX_PIN_LEN + 1] = "";
uint8_t pinLen = 0;
int wrongAttempts = 0;
bool lockedOut = false;
bool accessSession = false;
unsigned long lastAuthorizedActivity = 0;
bool enrollmentAuthorized = false;
unsigned long enrollmentAuthorizedAt = 0;
bool discardHostCommand = false;
// Three-factor build state (declared unconditionally so shared helpers compile in both builds).
bool bootstrapSession = false;  // enrollment-only session opened by the bootstrap PIN while the user store is empty
bool haveUsers = false;
uint8_t pendingPinId = 0;
unsigned long pendingPinAt = 0;
bool registerFailure(const char* eventName);
uint8_t matchedSlot();
void clearPIN();
#if HSS_REQUIRE_FACTORS
// Types/constants live up here so the Arduino auto-generated prototypes can see them.
const uint8_t MAX_USERS = 16;                       // fingerprint IDs 1..MAX_USERS
const uint32_t PIN_KDF_ITER = 5000;
const uint8_t USER_PIN_MIN = 4, USER_PIN_MAX = 8;
const uint8_t REC_MAGIC = 0xA5;
const int EE_BASE = 16;
const unsigned long FACE_WAIT_MS = 25000UL, PIN_WAIT_MS = 30000UL, PENDING_PIN_MS = 120000UL;

struct PinRecord {
  uint8_t magic;
  uint8_t reserved[3];
  uint32_t iter;
  uint8_t salt[16];
  uint8_t hash[32];
};

#endif

// Host file-transfer command buffer. Commands are accepted only during an
// authenticated STM32 session. Relative paths may include subdirectories.
const uint16_t MAX_SD_RELATIVE_PATH = 120;
char hostCommand[160];
uint8_t hostCommandLen = 0;

// Host script expects these exact unprefixed protocol lines:
// FACE_SCAN, FINGERPRINT_SCAN, PROTECTED_DATA_BEGIN, PROTECTED_DATA_END.
// Debug messages use DBG: prefix so the host script can ignore them.

// ---------------- OLED helpers ----------------
void showMessage(const char* line1, const char* line2) {
  display.clearDisplay();
  display.setTextColor(SSD1306_WHITE);
  display.setTextSize(2);
  display.setCursor(0, 5);
  display.println(line1);
  display.setCursor(0, 32);
  display.println(line2);
  display.display();
}

void showEnterPIN() {
  display.clearDisplay();
  display.setTextColor(SSD1306_WHITE);
  display.setTextSize(2);
  display.setCursor(0, 0);
  display.println("ENTER PIN");
  display.setCursor(0, 32);
  if (pinLen == 0) {
    display.println("_");
  } else {
    for (uint8_t i = 0; i < pinLen; i++) display.print('*');
  }
  display.display();
}

void showAccessMenu() {
  display.clearDisplay();
  display.setTextColor(SSD1306_WHITE);
  display.setTextSize(1);
  display.setCursor(0, 0);
  display.println("AUTHENTICATED");
  display.println();
  display.println("D: Read protected");
  display.println("   SD file");
  display.println();
  display.println("A: Arm fingerprint enroll");
  display.println("C: End session");
  display.display();
}

// ---------------- SD card ----------------
void configureSPIOnce() {
  if (spiConfigured) return;
  SPI.setMOSI(PA7);
  SPI.setMISO(PA6);
  SPI.setSCLK(PA5);
  SPI.begin();
  pinMode(SD_CS, OUTPUT);
  digitalWrite(SD_CS, HIGH);
  spiConfigured = true;
}

bool initializeSD(uint8_t attempts, bool powerUpWait) {
  configureSPIOnce();
  digitalWrite(SD_CS, HIGH);
  if (powerUpWait) delay(SD_POWERUP_DELAY_MS);

  for (uint8_t attempt = 1; attempt <= attempts; attempt++) {
    Serial.print("DBG: SD attempt ");
    Serial.println(attempt);
#if USE_SD_END
    SD.end();
#endif
    digitalWrite(SD_CS, HIGH);
    delay(50);

    if (SD.begin(SD_CS)) {
      Serial.println("DBG: SD INIT OK");
      File f = SD.open(PROTECTED_FILE, FILE_READ);
      if (f) {
        Serial.println("DBG: PROTECTED FILE FOUND");
        f.close();
      } else {
        Serial.println("DBG: WARNING PROTECTED FILE NOT FOUND");
      }
      return true;
    }
    Serial.println("DBG: SD INIT FAILED");
    delay(SD_RETRY_DELAY_MS);
  }

  Serial.println("DBG: SD UNAVAILABLE");
  return false;
}

bool ensureSD(bool force) {
  if (sdReady) return true;
  unsigned long now = millis();
  if (!force && (now - lastSdRetry) < SD_REINIT_INTERVAL_MS) return false;
  lastSdRetry = now;
  sdReady = initializeSD(2, false);
  return sdReady;
}

// ---------------- Audit log ----------------
// Appends events without storing PIN digits or biometric data.
// Timestamp is milliseconds since boot, not wall-clock time.
// HSS-S3: echo=false writes the audit entry without the "DBG: EVENT" serial line (used for
// PIN_VERIFIED, which is superseded by AUTH_PIN_VERIFIED:<slot>; see docs/SERIAL_PROTOCOL.md).
void logEventEx(const char* eventName, bool echo) {
  if (echo) {
    Serial.print("DBG: EVENT ");
    Serial.println(eventName);
  }

  if (!ensureSD(false)) {
    Serial.println("DBG: AUDIT LOG UNAVAILABLE");
    return;
  }

  // Diagnostic: show whether SD is believed ready immediately before opening audit.log.
  Serial.println("DBG: SD READY; opening audit.log");
  File f = SD.open(AUDIT_FILE, FILE_WRITE);
  if (!f) {
    Serial.println("DBG: AUDIT LOG OPEN FAILED");
    Serial.println("DBG: Marking SD unavailable; next event will retry initialization");
    sdReady = false;
    return;
  }
  Serial.println("DBG: AUDIT LOG OPEN OK");

  f.print("millis=");
  f.print(millis());
  f.print(",event=");
  f.println(eventName);
  f.flush();
  f.close();
}

void logEvent(const char* eventName) { logEventEx(eventName, true); }

// ---------------- Keypad ----------------
char scanKeypad() {
  for (uint8_t row = 0; row < 4; row++) {
    for (uint8_t r = 0; r < 4; r++) digitalWrite(rowPins[r], HIGH);
    digitalWrite(rowPins[row], LOW);
    delayMicroseconds(100);

    for (uint8_t col = 0; col < 4; col++) {
      if (digitalRead(colPins[col]) == LOW) {
        delay(20);
        if (digitalRead(colPins[col]) != LOW) continue;
        while (digitalRead(colPins[col]) == LOW) delay(5);
        delay(20);
        digitalWrite(rowPins[row], HIGH);
        return keyMap[row][col];
      }
    }
  }
  return '\0';
}

// ---------------- Face verification over USB serial ----------------
void flushSerialInput() {
  while (Serial.available()) Serial.read();
}

bool verifyFace() {
  showMessage("FACE", "SCAN...");
  flushSerialInput();
  Serial.println("FACE_SCAN");

  char line[32];
  uint8_t len = 0;
  unsigned long startTime = millis();

  while (millis() - startTime < 30000UL) {
    while (Serial.available()) {
      char c = Serial.read();
      if (c == '\r') continue;

      if (c == '\n') {
        line[len] = '\0';
        len = 0;

        if (strcmp(line, "FACE_VERIFIED") == 0) {
          logEvent("FACE_VERIFIED");
          return true;
        }
        if (strcmp(line, "FACE_FAILED") == 0) {
          logEvent("FACE_FAILED");
          return false;
        }
      } else if (len < sizeof(line) - 1) {
        line[len++] = c;
      } else {
        len = 0;
      }
    }
    delay(10);
  }

  logEvent("FACE_TIMEOUT");
  return false;
}

// ---------------- Fingerprint verification ----------------
bool verifyFingerprint() {
  showMessage("FINGER", "SCAN NOW");
  Serial.println("FINGERPRINT_SCAN");

  unsigned long startTime = millis();
  uint8_t result = FINGERPRINT_NOFINGER;

  while (millis() - startTime < 10000UL) {
    result = finger.getImage();
    if (result == FINGERPRINT_OK) break;

    if (result != FINGERPRINT_NOFINGER) {
      Serial.print("DBG: FP_GET_IMAGE_ERROR=");
      Serial.println(result);
      logEvent("FINGERPRINT_SENSOR_ERROR");
      return false;
    }
    delay(100);
  }

  if (result != FINGERPRINT_OK) {
    Serial.println("DBG: FP_GET_IMAGE_TIMEOUT");
    logEvent("FINGERPRINT_TIMEOUT");
    return false;
  }
  Serial.println("DBG: FP_GET_IMAGE_OK");

  result = finger.image2Tz(1);
  if (result != FINGERPRINT_OK) {
    Serial.print("DBG: FP_IMAGE2TZ_ERROR=");
    Serial.println(result);
    logEvent("FINGERPRINT_PROCESSING_FAILED");
    return false;
  }
  Serial.println("DBG: FP_IMAGE2TZ_OK");

  result = finger.fingerFastSearch();
  if (result == FINGERPRINT_OK) {
    Serial.print("DBG: FP_SEARCH_MATCH_ID=");
    Serial.println(finger.fingerID);
    Serial.print("DBG: FP_SEARCH_CONFIDENCE=");
    Serial.println(finger.confidence);
    logEvent("FINGERPRINT_VERIFIED");
    return true;
  }

  Serial.print("DBG: FP_SEARCH_ERROR=");
  Serial.println(result);
  if (result == FINGERPRINT_NOTFOUND) {
    Serial.println("DBG: FP_TEMPLATE_NOT_MATCHED");
  }
  logEvent("FINGERPRINT_FAILED");
  return false;
}

// ---------------- Session and temporary authorization ----------------
void clearEnrollmentAuthorization() {
  enrollmentAuthorized = false;
  enrollmentAuthorizedAt = 0;
}

void revokeAccessSession(const char* eventName) {
  bool wasActive = accessSession;
  accessSession = false;
  bootstrapSession = false;
  pendingPinId = 0;
  activeSlot = 0;
  lastAuthorizedActivity = 0;
  clearEnrollmentAuthorization();
  clearPIN();
  if (wasActive && eventName != nullptr) logEvent(eventName);
}

void touchAuthorizedSession() {
  if (accessSession) lastAuthorizedActivity = millis();
}

void checkSessionExpiry() {
  if (accessSession && (millis() - lastAuthorizedActivity >= SESSION_TIMEOUT_MS)) {
    revokeAccessSession("ACCESS_SESSION_EXPIRED");
    showMessage("SESSION", "EXPIRED");
    Serial.println("SESSION_EXPIRED");
    delay(800);
    showEnterPIN();
  }
  if (enrollmentAuthorized &&
      (millis() - enrollmentAuthorizedAt >= ENROLL_AUTH_TIMEOUT_MS)) {
    clearEnrollmentAuthorization();
    Serial.println("DBG: ENROLL_AUTH_EXPIRED");
  }
}

// ---------------- Protected file ----------------
void readProtectedFile() {
  if (!accessSession) {
    Serial.println("DBG: UNAUTHORIZED FILE READ BLOCKED");
    showMessage("ACCESS", "DENIED");
    logEvent("UNAUTHORIZED_FILE_READ_BLOCKED");
    return;
  }

  touchAuthorizedSession();
  // Try again if the card was absent at boot or became unavailable.
  if (!ensureSD(true)) {
    showMessage("SD CARD", "ERROR");
    logEvent("FILE_READ_FAILED_SD_UNAVAILABLE");
    delay(1500);
    showAccessMenu();
    return;
  }

  File f = SD.open(PROTECTED_FILE, FILE_READ);
  if (!f) {
    showMessage("FILE", "NOT FOUND");
    logEvent("PROTECTED_FILE_OPEN_FAILED");
    delay(1500);
    showAccessMenu();
    return;
  }

  showMessage("FILE", "READING");
  logEvent("PROTECTED_FILE_READ");
  Serial.println("PROTECTED_DATA_BEGIN");

  while (f.available()) {
    Serial.write(f.read());
  }
  f.close();

  Serial.println();
  Serial.println("PROTECTED_DATA_END");
  logEvent("PROTECTED_FILE_READ_COMPLETE");

  showMessage("FILE", "READ DONE");
  delay(1500);
  showAccessMenu();
}

// ---------------- Host SD-card file protocol ----------------
// Protocol:
//   PC -> STM32: LIST_FILES\n
//   STM32 -> PC: SD_FILE_LIST_BEGIN, one relative path per line, SD_FILE_LIST_END
//   PC -> STM32: READ_FILE:<relative-path>\n
//   STM32 -> PC: SD_FILE_BEGIN:<relative-path>:<size>, exactly <size> raw bytes,
//               then SD_FILE_END
//
// All file extensions are listed. Directories are traversed recursively.
// Relative paths use forward slashes, e.g. recordings/voice.wav.

bool isSafeRelativePath(const char* name) {
  if (name == nullptr || name[0] == '\0') return false;
  size_t len = strlen(name);
  if (len > MAX_SD_RELATIVE_PATH || name[0] == '/' || name[0] == '\\') return false;
  if (strchr(name, '\\') != nullptr || strchr(name, ':') != nullptr) return false;

  // Reject control characters and unsafe/empty path segments.
  const char* segment = name;
  for (size_t i = 0; i <= len; i++) {
    unsigned char c = (unsigned char)name[i];
    if (c == '/' || c == '\0') {
      size_t segLen = (size_t)(&name[i] - segment);
      if (segLen == 0) return false;
      if (segLen == 1 && segment[0] == '.') return false;
      if (segLen == 2 && segment[0] == '.' && segment[1] == '.') return false;
      segment = &name[i + 1];
    } else if (c < 32) {
      return false;
    }
  }
  return true;
}

void listDirectoryRecursive(File& directory, const char* parentPath, uint8_t depth) {
  // Defensive limit in case a malformed filesystem contains unexpected nesting.
  if (depth > 8) return;

  File entry = directory.openNextFile();
  while (entry) {
    const char* entryName = entry.name();
    char relativePath[MAX_SD_RELATIVE_PATH + 1];
    int written;

    if (entryName == nullptr || entryName[0] == '\0') {
      entry.close();
      entry = directory.openNextFile();
      continue;
    }

    if (parentPath == nullptr || parentPath[0] == '\0') {
      written = snprintf(relativePath, sizeof(relativePath), "%s", entryName);
    } else {
      written = snprintf(relativePath, sizeof(relativePath), "%s/%s", parentPath, entryName);
    }

    if (written > 0 && written < (int)sizeof(relativePath) &&
        isSafeRelativePath(relativePath)) {
      if (entry.isDirectory()) {
        listDirectoryRecursive(entry, relativePath, depth + 1);
      } else {
        // No extension filter: images, PDFs, videos, audio, and unknown types
        // are all sent to the host.
        Serial.println(relativePath);
      }
    } else {
      Serial.println("DBG: Skipped path that is too long or unsafe");
    }

    entry.close();
    entry = directory.openNextFile();
  }
}

void listSdFilesToHost() {
  if (!accessSession) {
    Serial.println("SD_ERROR:AUTH_REQUIRED");
    logEvent("HOST_FILE_LIST_BLOCKED");
    return;
  }
  touchAuthorizedSession();
  if (!ensureSD(true)) {
    Serial.println("SD_ERROR:CARD_UNAVAILABLE");
    logEvent("HOST_FILE_LIST_SD_FAILED");
    return;
  }

  File root = SD.open("/");
  if (!root) {
    Serial.println("SD_ERROR:ROOT_OPEN_FAILED");
    logEvent("HOST_FILE_LIST_ROOT_FAILED");
    return;
  }

  Serial.println("SD_FILE_LIST_BEGIN");
  listDirectoryRecursive(root, "", 0);
  root.close();
  Serial.println("SD_FILE_LIST_END");
  logEvent("HOST_FILE_LIST_COMPLETE");
}

void sendSdFileToHost(const char* filename) {
  if (!accessSession) {
    Serial.println("SD_ERROR:AUTH_REQUIRED");
    logEvent("HOST_FILE_READ_BLOCKED");
    return;
  }
  touchAuthorizedSession();
  if (!isSafeRelativePath(filename)) {
    Serial.println("SD_ERROR:INVALID_FILENAME");
    logEvent("HOST_FILE_READ_INVALID_NAME");
    return;
  }
  if (!ensureSD(true)) {
    Serial.println("SD_ERROR:CARD_UNAVAILABLE");
    logEvent("HOST_FILE_READ_SD_FAILED");
    return;
  }

  File f = SD.open(filename, FILE_READ);
  if (!f || f.isDirectory()) {
    if (f) f.close();
    Serial.println("SD_ERROR:FILE_NOT_FOUND");
    logEvent("HOST_FILE_READ_NOT_FOUND");
    return;
  }

  // Length-prefixed transfer preserves arbitrary binary bytes, including
  // nulls and newline characters. Zero-byte files are valid too.
  const uint32_t fileSize = (uint32_t)f.size();
  Serial.print("SD_FILE_BEGIN:");
  Serial.print(filename);
  Serial.print(":");
  Serial.println(fileSize);

  uint8_t buffer[128];
  uint32_t remaining = fileSize;
  while (remaining > 0) {
    uint32_t chunkSize = remaining > sizeof(buffer) ? sizeof(buffer) : remaining;
    uint32_t readCount = f.read(buffer, chunkSize);
    if (readCount == 0) {
      f.close();
      Serial.println();
      Serial.println("SD_ERROR:READ_FAILED");
      logEvent("HOST_FILE_READ_FAILED");
      return;
    }
    Serial.write(buffer, readCount);
    remaining -= readCount;
  }
  f.close();

  Serial.println("SD_FILE_END");
  logEvent("HOST_FILE_READ_COMPLETE");
}


// Read one newline-terminated ASCII command with a timeout while receiving a file.
bool readSerialLineTimed(char* out, size_t outSize, unsigned long timeoutMs) {
  if (out == nullptr || outSize < 2) return false;
  size_t len = 0;
  unsigned long started = millis();
  while (millis() - started < timeoutMs) {
    while (Serial.available() > 0) {
      int value = Serial.read();
      if (value < 0) break;
      char c = (char)value;
      if (c == '\r') continue;
      if (c == '\n') { out[len] = '\0'; return true; }
      if (len < outSize - 1) out[len++] = c;
      else return false;
    }
    delay(1);
  }
  return false;
}

bool readSerialExact(uint8_t* buffer, size_t n, unsigned long timeoutMs) {
  size_t got = 0;
  unsigned long lastByteAt = millis();
  while (got < n) {
    while (Serial.available() > 0 && got < n) {
      int value = Serial.read();
      if (value < 0) break;
      buffer[got++] = (uint8_t)value;
      lastByteAt = millis();
    }
    if (got >= n) return true;
    if (millis() - lastByteAt > timeoutMs) return false;
    delay(1);
  }
  return true;
}

// ---------------- Host-to-SD physical file upload ----------------
// Protocol: PC sends WRITE_FILE:<filename>:<size>\n, then exactly <size>
// raw bytes. This first implementation intentionally supports root filenames
// only and refuses to overwrite an existing SD-card file.
bool receiveHostFileToSD(const char* filename, uint32_t fileSize) {
  if (!accessSession) { Serial.println("SD_ERROR:AUTH_REQUIRED"); logEvent("HOST_FILE_WRITE_BLOCKED"); return false; }
  touchAuthorizedSession();
  if (!isSafeRelativePath(filename) || strchr(filename, '/') != nullptr) { Serial.println("SD_ERROR:INVALID_FILENAME"); logEvent("HOST_FILE_WRITE_INVALID_NAME"); return false; }
  if (fileSize > 8UL * 1024UL * 1024UL) { Serial.println("SD_ERROR:FILE_TOO_LARGE"); logEvent("HOST_FILE_WRITE_TOO_LARGE"); return false; }
  if (!ensureSD(true)) { Serial.println("SD_ERROR:CARD_UNAVAILABLE"); logEvent("HOST_FILE_WRITE_SD_FAILED"); return false; }
  if (SD.exists(filename)) { Serial.println("SD_ERROR:DESTINATION_EXISTS"); logEvent("HOST_FILE_WRITE_DESTINATION_EXISTS"); return false; }

  File destination = SD.open(filename, FILE_WRITE);
  if (!destination) { Serial.println("SD_ERROR:DESTINATION_OPEN_FAILED"); logEvent("HOST_FILE_WRITE_OPEN_FAILED"); return false; }
  Serial.print("SD_WRITE_READY:"); Serial.print(filename); Serial.print(":"); Serial.println(fileSize);

  // Stop-and-wait: host sends a chunk header and raw bytes, then waits for ACK.
  const uint16_t MAX_CHUNK = 512;
  uint8_t buffer[MAX_CHUNK];
  uint32_t received = 0, writtenTotal = 0, expectedSequence = 0;
  bool writeOK = true;
  const char* failureReason = "UNKNOWN";

  while (received < fileSize) {
    char chunkCommand[48];
    if (!readSerialLineTimed(chunkCommand, sizeof(chunkCommand), 30000UL)) { writeOK = false; failureReason = "CHUNK_HEADER_TIMEOUT"; break; }
    unsigned long sequence = 0, chunkLength = 0;
    char extra = '\0';
    int parsed = sscanf(chunkCommand, "WRITE_CHUNK:%lu:%lu%c", &sequence, &chunkLength, &extra);
    uint32_t remaining = fileSize - received;
    if (parsed != 2 || sequence != expectedSequence || chunkLength == 0 || chunkLength > MAX_CHUNK || chunkLength > remaining) {
      writeOK = false; failureReason = "INVALID_CHUNK_HEADER"; break;
    }

    // Explicitly tell the host that the header was parsed and the receiver is
    // waiting for this exact payload. The host must not send bytes before this.
    Serial.print("WRITE_CHUNK_READY:");
    Serial.print(sequence);
    Serial.print(":");
    Serial.println(chunkLength);

    if (!readSerialExact(buffer, (size_t)chunkLength, 10000UL)) { writeOK = false; failureReason = "CHUNK_DATA_TIMEOUT"; break; }
    size_t written = destination.write(buffer, (size_t)chunkLength);
    if (written != chunkLength) { writeOK = false; failureReason = "SD_WRITE_SHORT"; break; }
    received += (uint32_t)chunkLength; writtenTotal += (uint32_t)written;
    Serial.print("WRITE_CHUNK_OK:"); Serial.print(sequence); Serial.print(":"); Serial.println(written);
    expectedSequence++;
  }
  destination.flush(); destination.close();
  if (!writeOK || received != fileSize || writtenTotal != fileSize) {
    Serial.print("DBG: WRITE_DIAG reason="); Serial.print(failureReason); Serial.print(",received="); Serial.print(received);
    Serial.print(",expected="); Serial.print(fileSize); Serial.print(",written="); Serial.println(writtenTotal);
    SD.remove(filename); Serial.println("SD_ERROR:WRITE_INCOMPLETE"); logEvent("HOST_FILE_WRITE_INCOMPLETE"); return false;
  }
  File verify = SD.open(filename, FILE_READ);
  if (!verify) { Serial.println("DBG: WRITE_DIAG reason=VERIFY_OPEN_FAILED"); SD.remove(filename); Serial.println("SD_ERROR:VERIFY_OPEN_FAILED"); logEvent("HOST_FILE_WRITE_VERIFY_FAILED"); return false; }
  uint32_t verifiedSize = (uint32_t)verify.size(); verify.close();
  if (verifiedSize != fileSize) {
    Serial.print("DBG: WRITE_DIAG reason=VERIFY_SIZE_MISMATCH,verified="); Serial.print(verifiedSize); Serial.print(",expected="); Serial.println(fileSize);
    SD.remove(filename); Serial.println("SD_ERROR:VERIFY_SIZE_MISMATCH"); logEvent("HOST_FILE_WRITE_VERIFY_FAILED"); return false;
  }
  Serial.print("SD_WRITE_OK:"); Serial.print(filename); Serial.print(":"); Serial.println(verifiedSize);
  logEvent("HOST_FILE_WRITE_COMPLETE"); return true;
}


// ---------------- Host-triggered AS608 enrollment ----------------
// Host command: ENROLL_FINGERPRINT:<username>
// The username is a label only; the trusted bridge stores the ID mapping.
//
// Important: choose a free slot BEFORE scanning/creating the model. Do not
// call loadModel() after createModel(), because it can replace char-buffer 1.
int findFreeFingerprintSlot() {
  uint8_t status = finger.getParameters();
  if (status != FINGERPRINT_OK || finger.capacity == 0) {
    Serial.print("DBG: FP_CAPACITY_READ_ERROR=");
    Serial.println(status);
    return -1;
  }

  // Probe individual slots; template count is not a reliable free-slot index.
  uint16_t maxSlots = finger.capacity;
  if (maxSlots > 127) maxSlots = 127;
  for (uint16_t id = 1; id <= maxSlots; id++) {
    uint8_t result = finger.loadModel(id);
    if (result == FINGERPRINT_NOTFOUND) return (int)id;
    if (result != FINGERPRINT_OK) {
      Serial.print("DBG: FP_SLOT_PROBE_ERROR id=");
      Serial.print(id);
      Serial.print(" status=");
      Serial.println(result);
      return -1;
    }
  }
  return -1;
}

void enrollFingerprintFromHost(const char* username) {
  if (!accessSession || lockedOut || !enrollmentAuthorized ||
      (millis() - enrollmentAuthorizedAt >= ENROLL_AUTH_TIMEOUT_MS)) {
    clearEnrollmentAuthorization();
    Serial.println("ENROLL_ERROR:LOCAL_AUTH_REQUIRED");
    logEvent("HOST_ENROLL_BLOCKED");
    return;
  }
  // Consume local authorization before starting; it is one-time only.
  clearEnrollmentAuthorization();
  touchAuthorizedSession();

  if (username == nullptr || username[0] == '\0') {
    Serial.println("ENROLL_ERROR:INVALID_USERNAME");
    return;
  }

  // Select the destination ID before createModel(), and never load a model
  // between createModel() and storeModel().
  int id = findFreeFingerprintSlot();
#if HSS_REQUIRE_FACTORS
  if (id > (int)MAX_USERS) {
    Serial.println("ENROLL_ERROR:TOO_MANY_USERS");
    showMessage("ENROLL", "USER LIMIT");
    return;
  }
#endif
  if (id < 1) {
    Serial.println("ENROLL_ERROR:NO_FREE_SLOT_OR_COUNT_READ");
    showMessage("ENROLL", "SLOT ERROR");
    return;
  }

  showMessage("ENROLL", "PLACE FINGER");
  Serial.println("ENROLL:PLACE_FINGER");
  uint8_t p = FINGERPRINT_NOFINGER;
  unsigned long started = millis();
  while (millis() - started < 15000UL) {
    p = finger.getImage();
    if (p == FINGERPRINT_OK) break;
    if (p != FINGERPRINT_NOFINGER) {
      Serial.print("DBG: FP_ENROLL_FIRST_IMAGE_ERROR=");
      Serial.println(p);
      Serial.println("ENROLL_ERROR:FIRST_IMAGE");
      return;
    }
    delay(100);
  }
  if (p != FINGERPRINT_OK) {
    Serial.println("ENROLL_ERROR:FIRST_TIMEOUT");
    return;
  }

  p = finger.image2Tz(1);
  if (p != FINGERPRINT_OK) {
    Serial.print("DBG: FP_ENROLL_FIRST_PROCESS_ERROR=");
    Serial.println(p);
    Serial.println("ENROLL_ERROR:FIRST_PROCESS");
    return;
  }

  showMessage("ENROLL", "REMOVE FINGER");
  Serial.println("ENROLL:REMOVE_FINGER");
  started = millis();
  bool fingerRemoved = false;
  while (millis() - started < 10000UL) {
    p = finger.getImage();
    if (p == FINGERPRINT_NOFINGER) {
      fingerRemoved = true;
      break;
    }
    if (p != FINGERPRINT_OK && p != FINGERPRINT_NOFINGER) {
      Serial.print("DBG: FP_ENROLL_REMOVE_ERROR=");
      Serial.println(p);
    }
    delay(100);
  }
  if (!fingerRemoved) {
    Serial.println("ENROLL_ERROR:REMOVE_TIMEOUT");
    return;
  }

  showMessage("ENROLL", "SCAN AGAIN");
  Serial.println("ENROLL:PLACE_AGAIN");
  started = millis();
  p = FINGERPRINT_NOFINGER;
  while (millis() - started < 15000UL) {
    p = finger.getImage();
    if (p == FINGERPRINT_OK) break;
    if (p != FINGERPRINT_NOFINGER) {
      Serial.print("DBG: FP_ENROLL_SECOND_IMAGE_ERROR=");
      Serial.println(p);
      Serial.println("ENROLL_ERROR:SECOND_IMAGE");
      return;
    }
    delay(100);
  }
  if (p != FINGERPRINT_OK) {
    Serial.println("ENROLL_ERROR:SECOND_TIMEOUT");
    return;
  }

  p = finger.image2Tz(2);
  if (p != FINGERPRINT_OK) {
    Serial.print("DBG: FP_ENROLL_SECOND_PROCESS_ERROR=");
    Serial.println(p);
    Serial.println("ENROLL_ERROR:SECOND_PROCESS");
    return;
  }

  p = finger.createModel();
  if (p != FINGERPRINT_OK) {
    Serial.print("DBG: FP_CREATE_MODEL_ERROR=");
    Serial.println(p);
    showMessage("ENROLL", "MISMATCH");
    Serial.println("ENROLL_ERROR:FINGER_MISMATCH");
    return;
  }

  // No loadModel() calls here: preserve the newly-created model in buffer 1.
  p = finger.storeModel((uint16_t)id);
  if (p != FINGERPRINT_OK) {
    Serial.print("DBG: FP_STORE_MODEL_ERROR=");
    Serial.println(p);
    Serial.println("ENROLL_ERROR:STORE_FAILED");
    return;
  }

  Serial.print("ENROLL_OK:");
  Serial.print(username);
  Serial.print(":");
  Serial.println(id);
#if HSS_REQUIRE_FACTORS
  pendingPinId = (uint8_t)id;  // the host may now run SET_PIN:<id> for this fresh template (120 s)
  pendingPinAt = millis();
#endif

  uint8_t countStatus = finger.getTemplateCount();
  Serial.print("DBG: FP_TEMPLATE_COUNT_AFTER_ENROLL=");
  if (countStatus == FINGERPRINT_OK) {
    Serial.println(finger.templateCount);
  } else {
    Serial.print("READ_ERROR:");
    Serial.println(countStatus);
  }

  // Verify the stored slot is loadable without altering the enrollment buffer
  // used above. This is only a storage check, not a biometric match test.
  uint8_t verifyStatus = finger.loadModel((uint16_t)id);
  Serial.print("DBG: FP_STORED_SLOT_LOAD_STATUS=");
  Serial.println(verifyStatus);

  showMessage("ENROLL", "SUCCESS");
}

#if HSS_REQUIRE_FACTORS
// ============ Three-factor authentication: fingerprint (device) -> face (host) -> PIN (device) ============
// See docs/THREE_FACTOR_DESIGN.md. The PIN is set on this keypad and stored ONLY as a salted PBKDF2-HMAC-SHA256
// record in internal flash (EEPROM emulation), never as plaintext and never on the SD card.
uint32_t saltCounter = 0;
unsigned long lastFingerPoll = 0;

bool validUserId(long id) { return id >= 1 && id <= (long)MAX_USERS; }
int recordAddr(uint8_t id) { return EE_BASE + (int)(id - 1) * (int)sizeof(PinRecord); }

bool readRecord(uint8_t id, PinRecord& r) {
  if (!validUserId(id)) return false;
  EEPROM.get(recordAddr(id), r);
  return r.magic == REC_MAGIC && r.iter >= 1000 && r.iter <= 100000;
}

bool writeRecord(uint8_t id, PinRecord& r) {
  EEPROM.put(recordAddr(id), r);
  eeprom_buffer_flush();
  PinRecord back;
  EEPROM.get(recordAddr(id), back);
  return memcmp(&back, &r, sizeof(r)) == 0;
}

void eraseRecord(uint8_t id) {
  PinRecord blank;
  memset(&blank, 0xFF, sizeof(blank));
  EEPROM.put(recordAddr(id), blank);
  eeprom_buffer_flush();
}

bool anyUserRecord() {
  PinRecord r;
  for (uint8_t id = 1; id <= MAX_USERS; id++) if (readRecord(id, r)) return true;
  return false;
}

// F401 has no hardware RNG: the salt only has to be UNIQUE per record (it is not secret).
void makeSalt(uint8_t salt[16]) {
  uint8_t seed[24];
  uint32_t words[6] = {HAL_GetUIDw0(), HAL_GetUIDw1(), HAL_GetUIDw2(), (uint32_t)micros(), (uint32_t)millis(), saltCounter++};
  memcpy(seed, words, sizeof(words));
  uint8_t digest[32];
  hss_sha256(seed, sizeof(seed), digest);
  memcpy(salt, digest, 16);
}

void showIdleScreen() {
  if (!haveUsers) showMessage("SETUP", "ENTER PIN");
  else showMessage("PLACE", "FINGER");
}

void showPinPrompt(const char* title, uint8_t n) {
  display.clearDisplay();
  display.setTextColor(SSD1306_WHITE);
  display.setTextSize(2);
  display.setCursor(0, 0);
  display.println(title);
  display.setCursor(0, 32);
  if (n == 0) display.println("_");
  else for (uint8_t i = 0; i < n; i++) display.print('*');
  display.display();
}

// Reads digits until '#'. '*' clears, 'C' cancels. Returns true only for a non-empty PIN entered with '#'.
bool readPinFromKeypad(char* out, uint8_t& len, const char* title, unsigned long timeoutMs) {
  len = 0;
  out[0] = '\0';
  unsigned long last = millis();
  showPinPrompt(title, 0);
  while (millis() - last < timeoutMs) {
    char k = scanKeypad();
    if (k == '\0') { delay(5); continue; }
    last = millis();
    if (k >= '0' && k <= '9') {
      if (len < USER_PIN_MAX) { out[len++] = k; out[len] = '\0'; }
      showPinPrompt(title, len);
    } else if (k == '*') {
      len = 0; out[0] = '\0';
      showPinPrompt(title, 0);
    } else if (k == 'C') {
      return false;
    } else if (k == '#') {
      if (len > 0) return true;
    }
  }
  return false;
}

bool verifyUserPin(uint8_t id, const char* pin, uint8_t len) {
  PinRecord r;
  uint8_t salt[16], derived[32];
  bool have = readRecord(id, r);
  if (!have) {  // same work as a real check so timing does not reveal whether the account has a PIN
    makeSalt(salt);
    hss_pbkdf2_sha256((const uint8_t*)pin, len, salt, 16, PIN_KDF_ITER, derived);
    return false;
  }
  hss_pbkdf2_sha256((const uint8_t*)pin, len, r.salt, 16, r.iter, derived);
  return hss_ct_equal(derived, r.hash, 32);
}

bool storeUserPin(uint8_t id, const char* pin, uint8_t len) {
  PinRecord r;
  memset(&r, 0, sizeof(r));
  r.magic = REC_MAGIC;
  r.iter = PIN_KDF_ITER;
  makeSalt(r.salt);
  hss_pbkdf2_sha256((const uint8_t*)pin, len, r.salt, 16, r.iter, r.hash);
  return writeRecord(id, r);
}

// Generic failure: the stage goes to the host audit log only (the desktop UI shows one generic message).
void failAuth(const char* stage) {
  Serial.print("AUTH_FAILED:");
  Serial.println(stage);
  char ev[24];
  snprintf(ev, sizeof(ev), "AUTH_FAILED_%s", stage);
  showMessage("ACCESS", "DENIED");
  bool locked = registerFailure(ev);  // counts toward the 3-strikes lockout (prints AUTH_LOCKED)
  if (!locked) { delay(1500); showIdleScreen(); }
}

bool waitFaceVerdict(uint8_t id) {
  showMessage("FACE", "LOOK AT PC");
  flushSerialInput();
  Serial.print("FACE_SCAN:");
  Serial.println(id);
  char okLine[24], noLine[24];
  snprintf(okLine, sizeof(okLine), "FACE_VERIFIED:%u", (unsigned)id);
  snprintf(noLine, sizeof(noLine), "FACE_DENIED:%u", (unsigned)id);
  char line[32];
  uint8_t n = 0;
  unsigned long t0 = millis();
  while (millis() - t0 < FACE_WAIT_MS) {
    while (Serial.available()) {
      char c = (char)Serial.read();
      if (c == '\r') continue;
      if (c == '\n') {
        line[n] = '\0';
        n = 0;
        if (strcmp(line, okLine) == 0) { logEvent("FACE_VERIFIED"); return true; }
        if (strncmp(line, "FACE_", 5) == 0) { logEvent("FACE_DENIED"); return false; }  // denied OR a different id
      } else if (n < sizeof(line) - 1) {
        line[n++] = c;
      } else {
        n = 0;
      }
    }
    delay(10);
  }
  logEvent("FACE_TIMEOUT");
  return false;
}

void threeFactorPoll() {
  if (millis() - lastFingerPoll < 150UL) return;
  lastFingerPoll = millis();
  if (finger.getImage() != FINGERPRINT_OK) return;  // no finger
  showMessage("FINGER", "READING");
  if (finger.image2Tz(1) != FINGERPRINT_OK) { failAuth("FP"); return; }
  uint8_t r = finger.fingerFastSearch();
  if (r != FINGERPRINT_OK || !validUserId(finger.fingerID)) { failAuth("FP"); return; }
  uint8_t id = (uint8_t)finger.fingerID;
  if (!waitFaceVerdict(id)) { failAuth("FACE"); return; }
  Serial.println("AUTH_STAGE:PIN");
  char pin[USER_PIN_MAX + 1];
  uint8_t len = 0;
  bool got = readPinFromKeypad(pin, len, "ENTER PIN", PIN_WAIT_MS);
  bool ok = got && verifyUserPin(id, pin, len);
  memset(pin, 0, sizeof(pin));
  if (!ok) { failAuth("PIN"); return; }
  wrongAttempts = 0;
  bootstrapSession = false;
  accessSession = true;
  activeSlot = id;
  lastAuthorizedActivity = millis();
  clearEnrollmentAuthorization();
  showMessage("ACCESS", "GRANTED");
  Serial.print("AUTH_OK:");
  Serial.println(id);
  logEvent("ACCESS_GRANTED");
  delay(1500);
  showAccessMenu();
}

// Empty store only: the build-time bootstrap PIN (HSS_PIN_SLOT1) opens an ENROLLMENT-ONLY session (no SD access).
void bootstrapAuth() {
  bool ok = (matchedSlot() == 1);
  clearPIN();
  if (!ok) { failAuth("BOOT"); return; }
  wrongAttempts = 0;
  accessSession = true;
  bootstrapSession = true;
  activeSlot = 0;
  lastAuthorizedActivity = millis();
  enrollmentAuthorized = true;
  enrollmentAuthorizedAt = millis();
  Serial.println("BOOTSTRAP_OK");
  logEvent("BOOTSTRAP_OK");
  showMessage("SETUP", "ENROLL ADMIN");
}

void setPinFromHost(const char* arg) {
  char* end = nullptr;
  long id = strtol(arg, &end, 10);
  if (end == arg || *end != '\0' || !validUserId(id)) { Serial.println("PIN_SET_ERROR:INVALID_ID"); return; }
  unsigned long now = millis();
  bool viaEnroll = (pendingPinId == (uint8_t)id) && (now - pendingPinAt < PENDING_PIN_MS);
  bool viaAuth = enrollmentAuthorized && (now - enrollmentAuthorizedAt < ENROLL_AUTH_TIMEOUT_MS);
  if (!accessSession || lockedOut || !(viaEnroll || viaAuth)) {
    Serial.println("PIN_SET_ERROR:LOCAL_AUTH_REQUIRED");
    logEvent("HOST_SET_PIN_BLOCKED");
    return;
  }
  if (finger.loadModel((uint16_t)id) != FINGERPRINT_OK) { Serial.println("PIN_SET_ERROR:NO_FINGERPRINT"); return; }
  touchAuthorizedSession();
  char p1[USER_PIN_MAX + 1], p2[USER_PIN_MAX + 1];
  uint8_t l1 = 0, l2 = 0;
  bool a = readPinFromKeypad(p1, l1, "NEW PIN", PIN_WAIT_MS);
  if (!a) { memset(p1, 0, sizeof(p1)); Serial.println("PIN_SET_ERROR:TIMEOUT"); showMessage("PIN", "CANCELLED"); delay(1000); showAccessMenu(); return; }
  if (l1 < USER_PIN_MIN) { memset(p1, 0, sizeof(p1)); Serial.println("PIN_SET_ERROR:TOO_SHORT"); showMessage("PIN", "TOO SHORT"); delay(1000); showAccessMenu(); return; }
  bool b = readPinFromKeypad(p2, l2, "CONFIRM PIN", PIN_WAIT_MS);
  bool same = b && l1 == l2 && memcmp(p1, p2, l1) == 0;
  if (!b) { memset(p1, 0, sizeof(p1)); memset(p2, 0, sizeof(p2)); Serial.println("PIN_SET_ERROR:TIMEOUT"); showAccessMenu(); return; }
  if (!same) { memset(p1, 0, sizeof(p1)); memset(p2, 0, sizeof(p2)); Serial.println("PIN_SET_ERROR:MISMATCH"); showMessage("PIN", "MISMATCH"); delay(1000); showAccessMenu(); return; }
  bool stored = storeUserPin((uint8_t)id, p1, l1);
  memset(p1, 0, sizeof(p1));
  memset(p2, 0, sizeof(p2));
  if (!stored) { Serial.println("PIN_SET_ERROR:STORE_FAILED"); showMessage("PIN", "STORE ERROR"); delay(1000); showAccessMenu(); return; }
  clearEnrollmentAuthorization();
  pendingPinId = 0;
  haveUsers = true;
  logEvent("USER_PIN_SET");
  Serial.print("PIN_SET_OK:");
  Serial.println(id);
  showMessage("PIN", "SAVED");
  delay(1000);
  if (bootstrapSession) {  // setup finished: the enrollment-only session ends; the normal 3-factor login takes over
    revokeAccessSession("BOOTSTRAP_DONE");
    showIdleScreen();
  } else {
    showAccessMenu();
  }
}

void deleteUserFromHost(const char* arg) {
  char* end = nullptr;
  long id = strtol(arg, &end, 10);
  if (end == arg || *end != '\0' || !validUserId(id)) { Serial.println("USER_ERROR:INVALID_ID"); return; }
  if (!accessSession || lockedOut || bootstrapSession || !enrollmentAuthorized ||
      (millis() - enrollmentAuthorizedAt >= ENROLL_AUTH_TIMEOUT_MS)) {
    clearEnrollmentAuthorization();
    Serial.println("USER_ERROR:LOCAL_AUTH_REQUIRED");
    logEvent("HOST_DELETE_USER_BLOCKED");
    return;
  }
  clearEnrollmentAuthorization();
  if ((uint8_t)id == activeSlot) { Serial.println("USER_ERROR:SELF"); return; }
  uint8_t d = finger.deleteModel((uint16_t)id);
  eraseRecord((uint8_t)id);
  haveUsers = anyUserRecord();
  logEvent("USER_DELETED");
  if (d != FINGERPRINT_OK) Serial.println("DBG: FP_DELETE_MODEL_NOT_OK");
  Serial.print("USER_DELETED:");
  Serial.println(id);
}
#endif  // HSS_REQUIRE_FACTORS

void dispatchHostCommand(const char* command) {
  // HSS-S3: VERSION is answered even when locked or unauthenticated; it reveals no data and lets the
  // desktop identify the protocol generation (FIRMWARE_ANALYSIS.md conflict 9: flashed build unproven).
  if (strcmp(command, "VERSION") == 0) {
    Serial.print("HSS_FW:");
    Serial.print(HSS_FW_VERSION);
    Serial.print(":AUTH=");
    Serial.print(HSS_REQUIRE_FACTORS ? "THREE_FACTOR" : "PIN_ONLY");
    Serial.println(HSS_REQUIRE_FACTORS ? ":SLOTS=16:REQID=1" : ":SLOTS=2:REQID=1");
    return;
  }
  if (lockedOut) {
    Serial.println("SD_ERROR:LOCKED_OUT");
    return;
  }
#if HSS_REQUIRE_FACTORS
  if (bootstrapSession && strncmp(command, "ENROLL_FINGERPRINT:", 19) != 0 && strncmp(command, "SET_PIN:", 8) != 0 &&
      strcmp(command, "END_SESSION") != 0) {
    Serial.println("SD_ERROR:AUTH_REQUIRED");  // bootstrap session is enrollment-only: no SD access
    return;
  }
  if (strncmp(command, "SET_PIN:", 8) == 0) {
    setPinFromHost(command + 8);
    return;
  }
  if (strncmp(command, "DELETE_USER:", 12) == 0) {
    deleteUserFromHost(command + 12);
    return;
  }
#endif
  if (strcmp(command, "END_SESSION") == 0) {
    revokeAccessSession("ACCESS_SESSION_ENDED_BY_HOST");
    showMessage("SESSION", "ENDED");
    showEnterPIN();
    Serial.println("SESSION_ENDED");
    return;
  }
  if (strncmp(command, "ENROLL_FINGERPRINT:", 19) == 0) {
    enrollFingerprintFromHost(command + 19);
    return;
  }
  if (strncmp(command, "WRITE_FILE:", 11) == 0) {
    // Format: WRITE_FILE:<filename>:<size>
    const char* payload = command + 11;
    const char* separator = strrchr(payload, ':');
    if (separator == nullptr || separator == payload || separator[1] == '\0') {
      Serial.println("SD_ERROR:INVALID_WRITE_COMMAND");
      return;
    }
    char filename[MAX_SD_RELATIVE_PATH + 1];
    size_t nameLen = (size_t)(separator - payload);
    if (nameLen == 0 || nameLen > MAX_SD_RELATIVE_PATH) {
      Serial.println("SD_ERROR:INVALID_FILENAME");
      return;
    }
    memcpy(filename, payload, nameLen);
    filename[nameLen] = '\0';
    char* endPtr = nullptr;
    unsigned long parsedSize = strtoul(separator + 1, &endPtr, 10);
    if (endPtr == separator + 1 || *endPtr != '\0' || parsedSize > 8UL * 1024UL * 1024UL) {
      Serial.println("SD_ERROR:INVALID_FILE_SIZE");
      return;
    }
    receiveHostFileToSD(filename, (uint32_t)parsedSize);
    return;
  }
  if (strcmp(command, "LIST_FILES") == 0) {
    listSdFilesToHost();
    return;
  }
  if (strncmp(command, "READ_FILE:", 10) == 0) {
    sendSdFileToHost(command + 10);
    return;
  }
  Serial.println("SD_ERROR:UNKNOWN_COMMAND");
}

// HSS-S3 request IDs (optional, backwards compatible):  "@<id> <COMMAND>"  with <id> = 1..9 digits.
// The device brackets that command's output with REQ_BEGIN:<id> ... REQ_END:<id>, so the host can
// match replies exactly and discard late output from a timed-out request (legacy protocol correlates
// by order only; FIRMWARE_ANALYSIS.md s3, s8). Commands without "@" behave exactly as before.
void processHostCommand(const char* command) {
  if (command[0] != '@') {
    dispatchHostCommand(command);
    return;
  }
  const char* p = command + 1;
  uint32_t id = 0;
  uint8_t digits = 0;
  while (*p >= '0' && *p <= '9' && digits < 9) {
    id = id * 10 + (uint32_t)(*p - '0');
    p++;
    digits++;
  }
  if (digits == 0 || id == 0 || *p != ' ' || p[1] == '\0') {
    Serial.println("SD_ERROR:INVALID_REQUEST_ID");
    return;
  }
  Serial.print("REQ_BEGIN:");
  Serial.println(id);
  dispatchHostCommand(p + 1);
  Serial.print("REQ_END:");
  Serial.println(id);
}

void pollHostCommands() {
  while (Serial.available()) {
    char c = (char)Serial.read();
    if (c == '\r') continue;
    if (c == '\n') {
      if (!discardHostCommand) {
        hostCommand[hostCommandLen] = '\0';
        if (hostCommandLen > 0) processHostCommand(hostCommand);
      }
      hostCommandLen = 0;
      discardHostCommand = false;
      continue;
    }
    if (discardHostCommand) continue;
    if (hostCommandLen < sizeof(hostCommand) - 1) {
      hostCommand[hostCommandLen++] = c;
    } else {
      hostCommandLen = 0;
      discardHostCommand = true;
      Serial.println("SD_ERROR:COMMAND_TOO_LONG");
    }
  }
}

// ---------------- PIN/authentication ----------------
void clearPIN() {
  memset(enteredPIN, 0, sizeof(enteredPIN));
  pinLen = 0;
}

// HSS-S3: returns the matching slot (1..2) or 0. Both slots are always compared in full
// (no early exit) so timing does not reveal which slot was closer.
uint8_t matchedSlot() {
  uint8_t found = 0;
  for (uint8_t slot = 0; slot < 2; slot++) {
    size_t expected = strlen(SLOT_PINS[slot]);
    uint8_t diff = (pinLen != expected) ? 1 : 0;
    for (size_t i = 0; i < expected; i++) {
      char entered = (i < pinLen) ? enteredPIN[i] : 0;
      diff |= (uint8_t)(entered ^ SLOT_PINS[slot][i]);
    }
    if (diff == 0) found = slot + 1;
  }
  return found;
}

// HSS-S3: setup-time sanity check; refuses to run with ambiguous or malformed slot PINs.
bool slotPinsValid() {
  for (uint8_t slot = 0; slot < 2; slot++) {
    size_t n = strlen(SLOT_PINS[slot]);
    if (n < 4 || n > MAX_PIN_LEN) return false;
    for (size_t i = 0; i < n; i++) {
      if (SLOT_PINS[slot][i] < '0' || SLOT_PINS[slot][i] > '9') return false;
    }
  }
  return strcmp(SLOT_PINS[0], SLOT_PINS[1]) != 0;
}

bool registerFailure(const char* eventName) {
  revokeAccessSession("SESSION_REVOKED_AFTER_AUTH_FAILURE");
  wrongAttempts++;
  logEvent(eventName);

  if (wrongAttempts >= MAX_ATTEMPTS) {
    lockedOut = true;
    Serial.println("AUTH_LOCKED");  // HSS-S3 (planned string); legacy lines follow
    clearEnrollmentAuthorization();
    logEvent("SYSTEM_LOCKED");
    showMessage("SYSTEM", "LOCKED");
    Serial.println("DBG: SYSTEM LOCKED");
    return true;
  }
  return false;
}

void authenticate() {
  // HSS-S3 FIX: compare the entered PIN BEFORE revoking the old session. revokeAccessSession() calls
  // clearPIN(), so revoking first wiped the entered PIN and every attempt was "INCORRECT" (this ordering
  // was also in the base sketch). Found on the user's first hardware flash, 2026-11-03.
  uint8_t slot = matchedSlot();
  bool pinOk = (slot != 0);
  clearPIN();
  // Never carry a previous session into a new authentication attempt.
  revokeAccessSession("SESSION_REVOKED_FOR_REAUTH");

  if (!pinOk) {
    Serial.print("AUTH_PIN_FAILED:");  // HSS-S3: attempt number (1..MAX_ATTEMPTS)
    Serial.println(wrongAttempts + 1);
    if (registerFailure("PIN_FAILED")) return;
    showMessage("PIN", "INCORRECT");
    delay(1500);
    return;
  }

  logEventEx("PIN_VERIFIED", false);  // HSS-S3: serial echo superseded by AUTH_PIN_VERIFIED:<slot>

#if HSS_REQUIRE_FACTORS
  if (!verifyFace()) {
    showMessage("FACE", "DENIED");
    Serial.println("RESULT: ACCESS DENIED");
    delay(1500);
    registerFailure("ACCESS_DENIED_FACE");
    return;
  }

  if (!verifyFingerprint()) {
    showMessage("FINGER", "DENIED");
    Serial.println("RESULT: ACCESS DENIED");
    delay(1500);
    registerFailure("ACCESS_DENIED_FINGERPRINT");
    return;
  }
#endif  // HSS_REQUIRE_FACTORS

  wrongAttempts = 0;
  accessSession = true;
  activeSlot = slot;
  lastAuthorizedActivity = millis();
  clearEnrollmentAuthorization();
  showMessage("ACCESS", "GRANTED");
  // HSS-S3: emitted only once the build's whole authentication sequence has succeeded
  // (PIN only, or PIN+face+fingerprint when HSS_REQUIRE_FACTORS=1). Slot only, never a role.
  Serial.print("AUTH_PIN_VERIFIED:");
  Serial.println(slot);
  Serial.println("RESULT: ACCESS GRANTED");
  logEvent("ACCESS_GRANTED");
  delay(1500);
  showAccessMenu();
}

// ---------------- Setup ----------------
void setup() {
  Serial.begin(115200);
  delay(2000);

  Serial.println("DBG: SD INITIALIZATION START");
  sdReady = initializeSD(3, true);

  for (uint8_t i = 0; i < 4; i++) {
    pinMode(rowPins[i], OUTPUT);
    digitalWrite(rowPins[i], HIGH);
    pinMode(colPins[i], INPUT_PULLUP);
  }

  Wire.setSCL(PB8);
  Wire.setSDA(PB9);
  Wire.begin();

  if (!display.begin(SSD1306_SWITCHCAPVCC, OLED_ADDR)) {
    Serial.println("DBG: OLED NOT FOUND");
    while (true) delay(1000);
  }
  showMessage("SYSTEM", "STARTING");

  if (!slotPinsValid()) {  // HSS-S3
    showMessage("PIN CONFIG", "INVALID");
    Serial.println("DBG: CONFIG ERROR: slot PINs invalid (need 2 distinct 4-12 digit PINs)");
    while (true) delay(1000);
  }

  finger.begin(57600);
  delay(100);

  if (!finger.verifyPassword()) {
    Serial.println("DBG: FINGERPRINT SENSOR NOT FOUND");
    logEvent("FINGERPRINT_SENSOR_NOT_FOUND");
#if HSS_REQUIRE_FACTORS
    showMessage("FINGER", "ERROR");
    while (true) delay(1000);  // base behaviour: sensor mandatory when it is part of authentication
#else
    Serial.println("DBG: continuing without fingerprint sensor (PIN-only build)");  // HSS-S3
#endif
  }

  Serial.println("DBG: Fingerprint sensor ready");
  uint8_t templateCountStatus = finger.getTemplateCount();
  Serial.print("DBG: FP_TEMPLATE_COUNT_STATUS=");
  Serial.println(templateCountStatus);
  if (templateCountStatus == FINGERPRINT_OK) {
    Serial.print("DBG: FP_TEMPLATE_COUNT=");
    Serial.println(finger.templateCount);
  }
  uint8_t paramsStatus = finger.getParameters();
  Serial.print("DBG: FP_PARAMETERS_STATUS=");
  Serial.println(paramsStatus);
  if (paramsStatus == FINGERPRINT_OK) {
    Serial.print("DBG: FP_SECURITY_LEVEL=");
    Serial.println(finger.security_level);
    Serial.print("DBG: FP_CAPACITY=");
    Serial.println(finger.capacity);
  }
  Serial.println("DBG: SYSTEM READY");
  logEvent("SYSTEM_READY");
#if HSS_REQUIRE_FACTORS
  haveUsers = anyUserRecord();
  Serial.println(haveUsers ? "DBG: USERS ENROLLED" : "DBG: NO USERS (bootstrap setup mode)");
  showIdleScreen();
#else
  showEnterPIN();
#endif
}

// ---------------- Main loop ----------------
void loop() {
  checkSessionExpiry();
  pollHostCommands();

  if (lockedOut) {
    delay(500);
    return;
  }

#if HSS_REQUIRE_FACTORS
  if (!accessSession && haveUsers) threeFactorPoll();  // fingerprint -> face -> PIN; the keypad is read inside
#endif
  char key = scanKeypad();
  if (key == '\0') return;

  // Do not print individual key presses; that would expose PIN digits.
  Serial.println("DBG: KEYPAD INPUT");

  if (accessSession) {
    if (key == 'D') {
      if (!bootstrapSession) readProtectedFile();
    } else if (key == 'A') {
      enrollmentAuthorized = true;
      enrollmentAuthorizedAt = millis();
      Serial.println("ENROLL_AUTHORIZED:60_SECONDS");
      showMessage("ENROLL", "AUTHORIZED");
      delay(800);
      showAccessMenu();
    } else if (key == 'C') {
      revokeAccessSession("ACCESS_SESSION_ENDED");
      showMessage("SESSION", "ENDED");
      delay(1000);
      showEnterPIN();
    } else {
      showAccessMenu();
    }
    return;
  }

#if HSS_REQUIRE_FACTORS
  if (haveUsers) return;  // normal operation: no keypad login outside threeFactorPoll()
#endif
  if (key >= '0' && key <= '9') {
    if (pinLen < MAX_PIN_LEN) {
      enteredPIN[pinLen++] = key;
      enteredPIN[pinLen] = '\0';
      showEnterPIN();
    }
  } else if (key == '*') {
    clearPIN();
    showEnterPIN();
  } else if (key == '#') {
#if HSS_REQUIRE_FACTORS
    bootstrapAuth();
    if (!lockedOut && !accessSession) showIdleScreen();
#else
    authenticate();
    if (!lockedOut && !accessSession) showEnterPIN();
#endif
  }
}
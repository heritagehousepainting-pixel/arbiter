import { describe, expect, it } from "vitest";
import { fmtExpiry } from "../OptionsPanel";

describe("fmtExpiry", () => {
  it("keeps the YEAR so long-dated LEAPS don't read as already-expired", () => {
    // The bug: "2027-06-17" used to render as "06/17", which looks like it
    // already passed. It must include the year.
    expect(fmtExpiry("2027-06-17")).toBe("06/17/27");
  });

  it("formats a same-year expiry with its year too", () => {
    expect(fmtExpiry("2026-12-18")).toBe("12/18/26");
  });

  it("degrades safely on missing/malformed input", () => {
    expect(fmtExpiry(null)).toBe("?");
    expect(fmtExpiry(undefined)).toBe("?");
    expect(fmtExpiry("")).toBe("?");
    expect(fmtExpiry("garbage")).toBe("garbage");
  });
});

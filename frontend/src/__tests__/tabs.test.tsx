import { waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { mockApi, ownerUser, platformUser, renderApp, signedIn } from "./helpers";

const DASH = { totals: { today_litres: "0", today_amount_by_currency: {}, open_alerts: 0, unexplained_loss_7d: "0", flagged_cash_shifts_7d: 0, open_shifts: 0 }, next_refill: null, stations: [], alerts: [] };

function stubReload() {
  const reload = vi.fn();
  vi.stubGlobal("location", { ...window.location, reload });
  return reload;
}

function fromAnotherTab(key: string, newValue: string | null) {
  window.dispatchEvent(new StorageEvent("storage", { key, newValue }));
}

describe("one browser, several tabs", () => {
  afterEach(() => vi.unstubAllGlobals());

  async function ownerOnDashboard() {
    signedIn();
    mockApi({ "GET /api/auth/me/": { body: { user: ownerUser, tenant: { id: "t1", name: "Fuel Co" } } }, "GET /api/dashboard/": { body: DASH } });
    renderApp("/");
    await waitFor(() => expect(localStorage.getItem("fs_user")).toBe(ownerUser.id));
  }

  it("starts afresh when another tab signs in as someone else, so one account's page never shows another's numbers", async () => {
    await ownerOnDashboard();
    const reload = stubReload();
    fromAnotherTab("fs_user", ownerUser.id); // the same person (a token refresh in another tab): nothing to do
    expect(reload).not.toHaveBeenCalled();
    fromAnotherTab("fs_user", platformUser.id);
    expect(reload).toHaveBeenCalledTimes(1);
  });

  it("starts afresh when another tab signs out", async () => {
    await ownerOnDashboard();
    const reload = stubReload();
    fromAnotherTab("fs_access", null);
    expect(reload).toHaveBeenCalledTimes(1);
  });
});

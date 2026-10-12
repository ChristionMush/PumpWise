import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { mockApi, ownerUser, platformUser, renderApp } from "./helpers";

async function fillAndSubmit(identifier: string, password: string) {
  const user = userEvent.setup();
  await user.type(screen.getByLabelText("Phone number or email"), identifier);
  await user.type(screen.getByLabelText("Password"), password);
  await user.click(screen.getByRole("button", { name: "Sign in" }));
}

const DASH = { totals: { today_litres: "0", today_amount_by_currency: {}, open_alerts: 0, unexplained_loss_7d: "0", flagged_cash_shifts_7d: 0, open_shifts: 0 }, next_refill: null, stations: [], alerts: [] };

describe("login", () => {
  it("shows the sign-in form with the Tendeka & Elichi branding when signed out", async () => {
    mockApi({});
    renderApp("/");
    expect(await screen.findByRole("heading", { name: "Sign in" })).toBeInTheDocument();
    expect(screen.getByText("Built by Tendeka & Elichi")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "PumpWise" })).toBeInTheDocument();
    expect(screen.getByAltText("PumpWise logo")).toHaveAttribute("src", "/icon.svg");
  });

  it("signs a platform admin in and lands on the businesses list", async () => {
    const { calls } = mockApi({
      "POST /api/auth/login/": { body: { access: "a", refresh: "r", user: platformUser } },
      "GET /api/auth/me/": { body: { user: platformUser, tenant: null } },
      "GET /api/platform/tenants/": { body: { count: 0, next: null, results: [] } },
    });
    renderApp("/");
    await fillAndSubmit("t@x.com", "secret-pass");
    expect(await screen.findByRole("heading", { name: "Businesses" })).toBeInTheDocument();
    expect(calls.find((c) => c.path === "/api/auth/login/")?.body).toEqual({ identifier: "t@x.com", password: "secret-pass" });
    expect(localStorage.getItem("fs_access")).toBe("a");
    expect(screen.getByText("No businesses yet.")).toBeInTheDocument();
  });

  it("shows a plain message and stays on the form for wrong details", async () => {
    mockApi({ "POST /api/auth/login/": { status: 401, body: { detail: "Wrong login details." } } });
    renderApp("/");
    await fillAndSubmit("t@x.com", "nope");
    expect(await screen.findByRole("alert")).toHaveTextContent("Wrong login details.");
    expect(localStorage.getItem("fs_access")).toBeNull();
  });

  it("shows a friendly message when there is no connection", async () => {
    const { fetchMock } = mockApi({});
    fetchMock.mockRejectedValue(new TypeError("Failed to fetch"));
    renderApp("/");
    await fillAndSubmit("t@x.com", "x");
    expect(await screen.findByRole("alert")).toHaveTextContent("No connection");
  });

  it("forces a user with a temporary password to choose a new one before anything else", async () => {
    const temp = { ...ownerUser, must_change_password: true };
    const { calls } = mockApi({
      "POST /api/auth/login/": { body: { access: "a", refresh: "r", user: temp } },
      "GET /api/auth/me/": { body: { user: temp, tenant: { id: "t1", name: "Msasa Fuel", slug: "m", status: "active" } } },
    });
    renderApp("/");
    await fillAndSubmit("0772123456", "TempPass12");
    expect(await screen.findByRole("heading", { name: "Choose your own password" })).toBeInTheDocument();
    await waitFor(() => expect(calls.some((c) => c.path === "/api/platform/tenants/")).toBe(false));
  });

  it("completes the forced change and moves on", async () => {
    let changed = false;
    const { calls } = mockApi({
      "GET /api/auth/me/": () => ({ body: { user: { ...ownerUser, must_change_password: !changed }, tenant: { id: "t1", name: "Msasa Fuel", slug: "m", status: "active" } } }),
      "POST /api/auth/change-password/": () => { changed = true; return { body: { access: "a2", refresh: "r2" } }; },
      "GET /api/dashboard/": { body: DASH },
    });
    localStorage.setItem("fs_access", "a"); localStorage.setItem("fs_refresh", "r");
    renderApp("/");
    const user = userEvent.setup();
    await user.type(await screen.findByLabelText("Temporary password"), "TempPass12");
    await user.type(screen.getByLabelText("New password"), "My-new-pass-77");
    await user.click(screen.getByRole("button", { name: "Save new password" }));
    expect(await screen.findByRole("heading", { name: "Today" })).toBeInTheDocument();
    expect(calls.find((c) => c.path === "/api/auth/change-password/")?.body).toEqual({ current_password: "TempPass12", new_password: "My-new-pass-77" });
    expect(localStorage.getItem("fs_access")).toBe("a2");
  });

  it("keeps platform pages away from non-platform users", async () => {
    localStorage.setItem("fs_access", "a"); localStorage.setItem("fs_refresh", "r");
    mockApi({ "GET /api/auth/me/": { body: { user: ownerUser, tenant: { id: "t1", name: "Msasa Fuel", slug: "m", status: "active" } } }, "GET /api/dashboard/": { body: DASH } });
    renderApp("/platform");
    expect(await screen.findByRole("heading", { name: "Today" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Businesses" })).not.toBeInTheDocument();
  });

  it("shows the lock screen to a suspended owner, with the subscription", async () => {
    localStorage.setItem("fs_access", "a"); localStorage.setItem("fs_refresh", "r");
    mockApi({
      "GET /api/auth/me/": { body: { user: ownerUser, tenant: { id: "t1", name: "Msasa Fuel", slug: "m", status: "suspended" } } },
      "GET /api/subscription/": { body: { status: "past_due", plan: "standard", paid_through: "2026-09-30", tenant_status: "suspended" } },
    });
    renderApp("/");
    expect(await screen.findByRole("heading", { name: "Account suspended" })).toBeInTheDocument();
    expect(await screen.findByText("2026-09-30")).toBeInTheDocument();
  });

  it("lets an attendant of a suspended business keep working (grace period)", async () => {
    localStorage.setItem("fs_access", "a"); localStorage.setItem("fs_refresh", "r");
    const attendant = { ...ownerUser, role: "attendant", full_name: "Tendai" };
    localStorage.setItem("fs_tutorial_done", "1");
    mockApi({ "GET /api/auth/me/": { body: { user: attendant, tenant: { id: "t1", name: "Msasa Fuel", slug: "m", status: "suspended" } } }, "GET /api/shifts/prefill/": { body: { stations: [] } } });
    renderApp("/");
    expect(await screen.findByText(/No stations yet/)).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Account suspended" })).not.toBeInTheDocument();
  });

  it("refreshes an expired access token once and retries", async () => {
    localStorage.setItem("fs_access", "old"); localStorage.setItem("fs_refresh", "r");
    let first = true;
    const { calls } = mockApi({
      "GET /api/auth/me/": () => {
        if (first) { first = false; return { status: 401, body: { detail: "expired" } }; }
        return { body: { user: platformUser, tenant: null } };
      },
      "POST /api/auth/refresh/": { body: { access: "new", refresh: "r2" } },
      "GET /api/platform/tenants/": { body: { count: 0, next: null, results: [] } },
    });
    renderApp("/");
    expect(await screen.findByRole("heading", { name: "Businesses" })).toBeInTheDocument();
    expect(calls.filter((c) => c.path === "/api/auth/refresh/")).toHaveLength(1);
    expect(localStorage.getItem("fs_access")).toBe("new");
  });
});

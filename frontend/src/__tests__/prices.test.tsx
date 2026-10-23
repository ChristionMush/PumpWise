import "fake-indexeddb/auto";
import { screen } from "@testing-library/react";
import { beforeEach, describe, expect, it } from "vitest";
import { resetOffline } from "../offline";
import { mockApi, ownerUser, renderApp, signedIn } from "./helpers";

const tenant = { id: "t1", name: "Msasa Fuel", slug: "m", status: "active" };
const attendant = { id: "a", role: "attendant", full_name: "Tendai", must_change_password: false, tenant_id: "t1" };
const DASH = { totals: { today_litres: "0", today_amount_by_currency: {}, open_alerts: 0, unexplained_loss_7d: "0", flagged_cash_shifts_7d: 0, open_shifts: 0 }, next_refill: null, stations: [], alerts: [] };
const hoursAgo = (h: number) => new Date(Date.now() - h * 3600e3).toISOString();
const note = { id: "p1", station: "st1", station_name: "Main", fuel: "Petrol", currency: "USD", price_per_litre: "1.7000", previous_price: "1.5000", effective_from: "2026-10-20T08:00:00Z", upcoming: false };

beforeEach(async () => { await resetOffline(); localStorage.clear(); localStorage.setItem("fs_tutorial_done", "1"); signedIn(); });

describe("price changes on the home screen", () => {
  it("tells the owner the new price, who set it and when", async () => {
    mockApi({
      "GET /api/auth/me/": { body: { user: ownerUser, tenant } }, "GET /api/dashboard/": { body: DASH },
      "GET /api/prices/announcements/": { body: { announcements: [{ ...note, set_by: "Farai", set_by_role: "manager", set_at: hoursAgo(2) }] } },
    });
    renderApp("/");
    const card = await screen.findByRole("region", { name: "Price changes" });
    expect(card).toHaveTextContent("Petrol: USD 1.70");
    expect(card).toHaveTextContent("(was 1.50)");
    expect(card).toHaveTextContent("Set by Farai (manager) on");
  });

  it("drops a price change from the owner's screen once it is a day old, even a list saved on the phone", async () => {
    const old = { ...note, set_by: "Farai", set_by_role: "manager", set_at: hoursAgo(30), effective_from: hoursAgo(30) };
    localStorage.setItem(`fs_price_news:${ownerUser.id}`, JSON.stringify([old]));
    mockApi({ "GET /api/auth/me/": { body: { user: ownerUser, tenant } }, "GET /api/dashboard/": { body: DASH }, "GET /api/prices/announcements/": { status: 503, body: {} } });
    renderApp("/");
    expect(await screen.findByRole("heading", { name: "Today" })).toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "Price changes" })).not.toBeInTheDocument();
  });

  it("shows an attendant the new price without who set it, and keeps it for when there is no signal", async () => {
    mockApi({
      "GET /api/auth/me/": { body: { user: attendant, tenant } }, "GET /api/shifts/prefill/": { body: { stations: [] } },
      "GET /api/prices/announcements/": { body: { announcements: [note] } },
    });
    renderApp("/");
    const card = await screen.findByRole("region", { name: "Price changes" });
    expect(card).toHaveTextContent("Petrol: USD 1.70");
    expect(card).not.toHaveTextContent("Set by");
    expect(JSON.parse(localStorage.getItem("fs_price_news:a")!)).toHaveLength(1);
  });

  it("shows nothing when no price changed", async () => {
    mockApi({ "GET /api/auth/me/": { body: { user: ownerUser, tenant } }, "GET /api/dashboard/": { body: DASH }, "GET /api/prices/announcements/": { body: { announcements: [] } } });
    renderApp("/");
    expect(await screen.findByRole("heading", { name: "Today" })).toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "Price changes" })).not.toBeInTheDocument();
  });
});

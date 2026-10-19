import { screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { mockApi, renderApp, signedIn } from "./helpers";

const manager = { id: "m", role: "manager", full_name: "Tatenda", email: null, phone: null, must_change_password: false, tenant_id: "t" };
const history = {
  every_hours: 24,
  tanks: [{ id: "t1", name: "Diesel tank", station: "Msasa Road", fuel_code: "DIESEL", last_dip_at: "2026-10-17T04:30:00Z", last_dip_by: "Tatenda", hours_since_last_dip: 50, overdue: true }],
  days: [
    { date: "2026-10-18", entries: [], missing: ["Msasa Road · Diesel tank"] },
    { date: "2026-10-17", entries: [
      { type: "dip", at: "2026-10-17T04:30:00Z", tank_id: "t1", tank: "Diesel tank", station: "Msasa Road", dip_mm: "620", litres: "9750", by: "Tatenda" },
      { type: "delivery", at: "2026-10-17T07:00:00Z", tank_id: "t1", tank: "Diesel tank", station: "Msasa Road", litres: "12000", supplier: "Zuva", note_number: "Z-9", by: "Tatenda" },
    ], missing: [] },
  ],
};

const row = (text: string) => (_: string, el: Element | null) => el?.tagName === "LI" && (el.textContent ?? "").includes(text);

describe("dip history", () => {
  it("shows overdue tanks, each dip and delivery by day, and the days a tank was not dipped", async () => {
    signedIn();
    mockApi({
      "GET /api/auth/me/": { body: { user: manager, tenant: { id: "t", name: "Msasa", slug: "m", status: "active" } } },
      "GET /api/stations/": { body: { results: [{ id: "s1", name: "Msasa Road" }] } },
      "GET /api/reports/dip-history/": { body: history },
    });
    renderApp("/dip-history");
    expect(await screen.findByRole("heading", { name: "Dip history" })).toBeInTheDocument();
    expect(await screen.findByText("dip overdue")).toBeInTheDocument();
    expect(screen.getByText(/2 days ago/)).toBeInTheDocument();
    expect(screen.getByText(row("Diesel tank: dip 620 mm = " + (9750).toLocaleString() + " L"))).toBeInTheDocument();
    expect(screen.getByText(row("Delivery to Diesel tank: " + (12000).toLocaleString() + " L from Zuva (note Z-9)"))).toBeInTheDocument();
    expect(screen.getByText("No dip: Msasa Road · Diesel tank")).toBeInTheDocument();
  });
});

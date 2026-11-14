import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { mockApi, renderApp, signedIn } from "./helpers";

const manager = { id: "m", role: "manager", full_name: "Tatenda", email: null, phone: null, must_change_password: false, tenant_id: "t" };
const tanks = [{ id: "k1", name: "Tank 1", fuel_code: "PETROL", has_calibration: false, calibration: [] }];
const routes = {
  "GET /api/auth/me/": { body: { user: manager, tenant: { id: "t", name: "Msasa", slug: "m", status: "active" } } },
  "GET /api/shifts/prefill/": { body: { stations: [{ id: "s1", name: "Msasa Road", pricing_currency: "USD", pumps: [], prices: [], open_shift_id: null, tanks }] } },
  "GET /api/day-close/": { body: [] },
};

describe("deliveries waiting for details", () => {
  it("lists a delivery saved without its supplier, value and note number, and adds them", async () => {
    const user = userEvent.setup();
    signedIn();
    let pending = [{ id: "d1", tank_name: "Tank 1", station_name: "Msasa Road", delivered_at: "2026-11-14T09:59:00Z", litres_received: "6000.000",
      supplier: "", note_number: "", value: null, station_currency: "USD" }];
    const { calls } = mockApi({
      ...routes,
      "GET /api/deliveries/": () => ({ body: pending }),
      "PATCH /api/deliveries/d1/": () => { pending = []; return { body: {} }; },
    });
    renderApp("/dip");
    const list = await screen.findByRole("region", { name: "Deliveries waiting for details" });
    const row = within(list).getByRole("form");
    expect(row).toHaveTextContent(/Msasa Road · Tank 1 · 6\D?000 L/); // thousands separator per locale
    await user.type(within(row).getByLabelText("Supplier"), "Kariba Fuel Supplies");
    await user.type(within(row).getByLabelText("Value (USD)"), "8460");
    await user.type(within(row).getByLabelText("Delivery note no."), "KFS-20431");
    await user.click(within(row).getByRole("button", { name: "Save details" }));
    await waitFor(() => expect(calls.find((c) => c.method === "PATCH")?.body).toEqual({ supplier: "Kariba Fuel Supplies", note_number: "KFS-20431", value: "8460" }));
    await waitFor(() => expect(screen.queryByRole("region", { name: "Deliveries waiting for details" })).not.toBeInTheDocument());
  });

  it("the delivery form on the Dip screen takes the invoice value", async () => {
    signedIn();
    mockApi({ ...routes, "GET /api/deliveries/": { body: [] } });
    renderApp("/dip");
    expect(await screen.findByLabelText("Value (USD)")).toHaveAttribute("placeholder", "Invoice total");
  });
});

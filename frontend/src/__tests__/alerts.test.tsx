import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { mockApi, ownerUser, renderApp, signedIn } from "./helpers";

const tenant = { id: "t1", name: "Msasa Fuel", slug: "m", status: "active" };
const manager = { ...ownerUser, id: "m", role: "manager", full_name: "Tatenda" };
const reasons = {
  loss: [
    { code: "meter_typo", label: "A meter number was mistyped: corrected", write_off: false },
    { code: "real_loss", label: "Real loss: fuel is missing", write_off: true },
    { code: "other", label: "Other (explain in the note)", write_off: false },
  ],
};
const loss = { id: "a1", kind: "loss", severity: "critical", message: "Msasa Road Petrol tank: 142 L unexplained.", station_name: "Msasa Road", status: "open",
  created_at: new Date(Date.now() - 5 * 36e5).toISOString(), reopened_count: 0, pattern: "3 fuel losses on this tank in 7 days: check the tank's calibration chart and dips." };

describe("alerts", () => {
  it("shows a tank that gained fuel in green, and says why an alert cleared by itself", async () => {
    signedIn();
    const gain = { ...loss, id: "a2", severity: "warning", message: "Msasa Road Petrol tank on 2026-10-21: tank gained fuel it should not have (-209.000 L).", gain: true, pattern: null };
    const cleared = { ...loss, id: "a3", status: "auto_resolved", pattern: null, resolution_note: "Cancelled out: the loss and the gain balance." };
    mockApi({
      "GET /api/auth/me/": { body: { user: manager, tenant } },
      "GET /api/alerts/": { body: { count: 2, results: [gain, cleared] } },
      "GET /api/alerts/summary/": { body: { open: 1, awaiting_approval: 0, to_review: 0, reasons } },
    });
    renderApp("/alerts");
    const card = (await screen.findByText(/tank gained fuel/)).closest("section")!;
    expect(card).toHaveClass("good");
    expect(card).toHaveTextContent("Fuel gain");
    expect(screen.getByText(/Cleared by itself:/).closest("p")).toHaveTextContent("Cancelled out");
  });

  it("lets a manager resolve an alert by saying how, and sends write-offs to the owner with a required note", async () => {
    signedIn();
    const { calls } = mockApi({
      "GET /api/auth/me/": { body: { user: manager, tenant } },
      "GET /api/alerts/": { body: { count: 1, results: [loss] } },
      "GET /api/alerts/summary/": { body: { open: 1, awaiting_approval: 0, to_review: 0, reasons } },
      "POST /api/alerts/a1/resolve/": { body: { ...loss, status: "awaiting_approval" } },
    });
    renderApp("/alerts");
    const user = userEvent.setup();
    expect(await screen.findByText(/142 L unexplained/)).toBeInTheDocument();
    expect(screen.getByText(/3 fuel losses on this tank/)).toBeInTheDocument();
    expect(screen.queryByRole("tab", { name: /To review/ })).not.toBeInTheDocument(); // owners only
    await user.click(screen.getByRole("button", { name: "Resolve" }));
    await user.selectOptions(screen.getByLabelText("How was it resolved?"), "real_loss");
    expect(screen.getByText(/goes to the owner for approval/)).toBeInTheDocument();
    const send = screen.getByRole("button", { name: "Send for approval" });
    expect(send).toBeDisabled(); // a write-off needs a note
    await user.type(screen.getByLabelText(/Note/), "Leak at the valve");
    await user.click(send);
    await waitFor(() => expect(calls.some((c) => c.method === "POST" && c.path === "/api/alerts/a1/resolve/")).toBe(true));
    expect(calls.find((c) => c.path === "/api/alerts/a1/resolve/")!.body).toEqual({ reason: "real_loss", note: "Leak at the valve" });
    expect(await screen.findByText("Sent to the owner for approval.")).toBeInTheDocument();
  });

  it("gives the owner a To review list to accept a manager's resolution or send it back with a note", async () => {
    signedIn();
    const resolved = { ...loss, status: "resolved", resolution_label: "A meter number was mistyped: corrected", resolution_note: "End was 100 not 1000",
      resolved_by_name: "Tatenda", resolved_by_role: "manager", reviewed_at: null, pattern: "" };
    const { calls } = mockApi({
      "GET /api/auth/me/": { body: { user: ownerUser, tenant } },
      "GET /api/alerts/": (_b, url) => ({ body: url.includes("view=review") ? { count: 1, results: [resolved] } : { count: 0, results: [] } }),
      "GET /api/alerts/summary/": { body: { open: 0, awaiting_approval: 0, to_review: 1, reasons } },
      "POST /api/alerts/a1/send-back/": { body: { ...resolved, status: "open" } },
      "GET /api/alerts/a1/": { body: { ...resolved, events: [{ id: "e1", kind: "opened", note: "x", data: {}, created_at: loss.created_at }, { id: "e2", kind: "resolved", actor_name: "Tatenda", note: "End was 100 not 1000", data: { label: "A meter number was mistyped: corrected" }, created_at: loss.created_at }] } },
    });
    renderApp("/alerts");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("tab", { name: "To review (1)" }));
    expect(await screen.findByText(/End was 100 not 1000/)).toBeInTheDocument();
    expect(screen.getByText(/Tatenda \(manager\)/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "What happened" }));
    expect(await screen.findByText(/Resolved: A meter number was mistyped: corrected · Tatenda/)).toBeInTheDocument();
    const back = screen.getByRole("button", { name: "Send back" });
    expect(back).toBeDisabled();
    await user.type(screen.getByLabelText(/Note \(required to send back\)/), "Show me the pump photo");
    await user.click(back);
    await waitFor(() => expect(calls.some((c) => c.path === "/api/alerts/a1/send-back/")).toBe(true));
    expect(calls.find((c) => c.path === "/api/alerts/a1/send-back/")!.body).toEqual({ note: "Show me the pump photo" });
    expect(screen.getByRole("button", { name: "Accept" })).toBeInTheDocument();
  });

  it("turns a missing delivery into a delivery form filled from the dips, saved with details left pending", async () => {
    const user = userEvent.setup();
    signedIn();
    const delivery = { id: "a9", kind: "delivery", severity: "warning", status: "open", station_name: "Msasa Road", reopened_count: 0, pattern: null,
      message: "Msasa Road Tank 1: about 6,000 L came in between the dips at 08:00 and 12:00, but no delivery is recorded. Record the delivery.",
      created_at: new Date(Date.now() - 36e5).toISOString(),
      delivery_form: { tank: "k1", tanks: [{ id: "k1", name: "Tank 1" }], fuel: "Petrol", litres: "6000", currency: "USD",
        from: "2026-11-14T06:00:00Z", to: "2026-11-14T10:00:00Z", delivered_at: "2026-11-14T09:59:00Z" } };
    const { calls } = mockApi({
      "GET /api/auth/me/": { body: { user: manager, tenant } },
      "GET /api/alerts/": { body: { count: 1, results: [delivery] } },
      "GET /api/alerts/summary/": { body: { open: 1, awaiting_approval: 0, to_review: 0, reasons } },
      "POST /api/alerts/a9/record-delivery/": { body: { ...delivery, status: "resolved", delivery_form: null } },
    });
    renderApp("/alerts");
    const form = await screen.findByRole("form", { name: "Record the delivery" });
    expect(form).toHaveTextContent("Petrol");
    expect(screen.getByLabelText("Litres received")).toHaveValue("6000");
    expect(screen.getByLabelText("Supplier")).toHaveAttribute("placeholder", "Pending");
    expect(screen.getByLabelText("Value (USD)")).toHaveValue("");
    expect(form).toHaveTextContent("Pending: supplier, value, delivery note number");
    await user.type(screen.getByLabelText("Supplier"), "Kariba Fuel Supplies");
    await user.click(screen.getByRole("button", { name: "Save delivery" }));
    await waitFor(() => expect(calls.find((c) => c.path === "/api/alerts/a9/record-delivery/")).toBeTruthy());
    const body = calls.find((c) => c.path === "/api/alerts/a9/record-delivery/")!.body;
    expect(body).toMatchObject({ tank: "k1", litres: "6000", supplier: "Kariba Fuel Supplies", value: null, note_number: "" });
    expect(new Date(body.delivered_at).getTime()).toBe(new Date("2026-11-14T09:59:00Z").getTime());
    expect(await screen.findByText(/Add the value, delivery note number on the Dip screen/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Not a delivery? Resolve another way" })).toBeInTheDocument();
  });

  it("splits a delivery into joined tanks by how much each tank's dip rose, and saves one part per tank", async () => {
    const user = userEvent.setup();
    signedIn();
    const delivery = { id: "a10", kind: "delivery", severity: "warning", status: "open", station_name: "Mverechena", reopened_count: 0, pattern: null,
      message: "Mverechena Petrol Tank 1 + Petrol Tank 2: about 7,192 L came in between the dips, but no delivery is recorded. Record the delivery.",
      created_at: new Date(Date.now() - 36e5).toISOString(),
      delivery_form: { tank: "k1", tanks: [{ id: "k1", name: "Petrol Tank 1" }, { id: "k2", name: "Petrol Tank 2" }], fuel: "Petrol", litres: "7192", currency: "USD",
        from: "2026-11-18T06:58:00Z", to: "2026-11-18T15:30:00Z", delivered_at: "2026-11-18T15:29:00Z",
        split: [{ tank: "k1", name: "Petrol Tank 1", rise: "1658", litres: "2023" }, { tank: "k2", name: "Petrol Tank 2", rise: "4237", litres: "5169" }] } };
    const { calls } = mockApi({
      "GET /api/auth/me/": { body: { user: manager, tenant } },
      "GET /api/alerts/": { body: { count: 1, results: [delivery] } },
      "GET /api/alerts/summary/": { body: { open: 1, awaiting_approval: 0, to_review: 0, reasons } },
      "POST /api/alerts/a10/record-delivery/": { body: { ...delivery, status: "resolved", delivery_form: null } },
    });
    renderApp("/alerts");
    const form = await screen.findByRole("form", { name: "Record the delivery" });
    expect(screen.queryByLabelText("Into tank")).not.toBeInTheDocument();
    expect(screen.getByLabelText(/^Petrol Tank 1 \(dip rose 1.658 L\)$/)).toHaveValue("2023");
    expect(screen.getByLabelText(/^Petrol Tank 2 \(dip rose 4.237 L\)$/)).toHaveValue("5169");
    expect(form).toHaveTextContent(/Total: 7.192 L/);
    const tank2 = screen.getByLabelText(/^Petrol Tank 2 \(dip rose 4.237 L\)$/);
    await user.clear(tank2);
    await user.type(tank2, "5000");
    expect(form).toHaveTextContent(/Total: 7.023 L/);
    await user.click(screen.getByRole("button", { name: "Save delivery" }));
    await waitFor(() => expect(calls.find((c) => c.path === "/api/alerts/a10/record-delivery/")).toBeTruthy());
    const body = calls.find((c) => c.path === "/api/alerts/a10/record-delivery/")!.body;
    expect(body.split).toEqual([{ tank: "k1", litres: "2023" }, { tank: "k2", litres: "5000" }]);
    expect(body.tank).toBeUndefined();
  });
});

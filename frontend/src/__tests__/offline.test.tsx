import "fake-indexeddb/auto";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it } from "vitest";
import { allOps, enqueue, expectedVsHanded, kvGet, litres, priceAt, resetOffline, syncNow, type PriceRow } from "../offline";
import { mockApi, renderApp, signedIn } from "./helpers";

const prices: PriceRow[] = [
  { fuel_type: "p", currency: "USD", price: "1.00", from: "2026-09-01T00:00:00Z", to: "2026-10-01T10:00:00Z" },
  { fuel_type: "p", currency: "USD", price: "1.20", from: "2026-10-01T10:00:00Z", to: null },
];

beforeEach(async () => { await resetOffline(); localStorage.clear(); });

describe("local calculation (works with no signal)", () => {
  it("uses the price in force at the time, including the boundary", () => {
    expect(priceAt(prices, "p", "USD", new Date("2026-09-15T00:00:00Z"))).toBe(1);
    expect(priceAt(prices, "p", "USD", new Date("2026-10-01T10:00:00Z"))).toBe(1.2);
    expect(priceAt(prices, "p", "ZWG", new Date("2026-10-01T10:00:00Z"))).toBeNull();
  });
  it("handles meter rollover and never invents litres for a backwards meter", () => {
    expect(litres(99999990, 10, 99999999.99)).toBeCloseTo(20, 2);
    expect(litres(500, 100, 99999999.99)).toBe(0);
  });
  it("shows expected, handed in and the difference", () => {
    const r = expectedVsHanded([{ fuel_type: "p", start: 1000, end: 1100, meter_max: 1e8 }], prices, "USD", new Date("2026-10-02T06:00:00Z"), [{ amount: 100, rate: 1 }, { amount: 540, rate: 0.037 }]);
    expect(r.expected).toBeCloseTo(120, 2);
    expect(r.submitted).toBeCloseTo(119.98, 2);
    expect(r.variance).toBeCloseTo(-0.02, 2);
  });
});

describe("offline queue", () => {
  it("keeps entries when there is no signal and sends them in order when it returns", async () => {
    signedIn();
    const sent: any[] = [];
    let online = false;
    const { fetchMock } = mockApi({ "POST /api/sync/": (body) => { sent.push(...body.ops); return { body: { results: body.ops.map((o: any) => ({ op_id: o.op_id, status: "applied" })) } }; } });
    fetchMock.mockImplementation(async (_u: any, init?: RequestInit) => {
      if (!online) throw new TypeError("Failed to fetch");
      const body = JSON.parse(String(init?.body));
      sent.push(...body.ops);
      return new Response(JSON.stringify({ results: body.ops.map((o: any) => ({ op_id: o.op_id, status: "applied" })) }), { status: 200 });
    });
    await enqueue("shift_start", { shift_id: "s1" });
    await enqueue("shift_end", { shift_id: "s1" });
    await syncNow();
    expect((await allOps()).map((o) => o.kind)).toEqual(["shift_start", "shift_end"]); // nothing lost while offline
    online = true;
    await syncNow();
    expect(sent.map((o) => o.kind)).toEqual(["shift_start", "shift_end"]);
    expect(await allOps()).toEqual([]);
  });

  it("resending after a lost response is safe: the same op_id is sent again, never a new one", async () => {
    signedIn();
    const ids: string[] = [];
    let attempt = 0;
    const { fetchMock } = mockApi({});
    fetchMock.mockImplementation(async (_u: any, init?: RequestInit) => {
      const body = JSON.parse(String(init?.body));
      ids.push(body.ops[0].op_id);
      if (++attempt === 1) throw new TypeError("connection dropped after sending");
      return new Response(JSON.stringify({ results: [{ op_id: body.ops[0].op_id, status: "applied", duplicate: true }] }), { status: 200 });
    });
    await enqueue("delivery", { tank: "t", litres: "100" });
    await syncNow();
    await syncNow();
    expect(ids).toHaveLength(2);
    expect(ids[0]).toBe(ids[1]);
    expect(await allOps()).toEqual([]);
  });

  it("drops rejected entries from the queue (the server keeps them for a manager) so the phone never loops", async () => {
    signedIn();
    mockApi({ "POST /api/sync/": (body) => ({ body: { results: body.ops.map((o: any) => ({ op_id: o.op_id, status: "rejected", error: "Shift already reviewed" })) } }) });
    await enqueue("shift_end", { shift_id: "old" });
    await syncNow();
    expect(await allOps()).toEqual([]);
  });
});

describe("attendant shift screen", () => {
  const station = {
    id: "st1", name: "Main", pricing_currency: "USD", open_shift_id: null, tanks: [],
    prices: [{ fuel_type: "p", currency: "USD", price: "2.00", from: "2026-01-01T00:00:00Z", to: null }],
    pumps: [{ id: "pu1", name: "Pump 1", nozzles: [{ id: "n1", number: 1, label: "Nozzle 1", meter_max: "99999999.99", last_reading: "1000.00", fuel_type: "p", fuel_code: "PETROL" }] }],
  };

  it("starts a shift with prefilled numbers, shows expected vs handed in live, and ends it, all saved on the phone first", async () => {
    signedIn(); localStorage.setItem("fs_tutorial_done", "1");
    const { calls } = mockApi({
      "GET /api/auth/me/": { body: { user: { id: "a", role: "attendant", full_name: "Tendai", must_change_password: false, tenant_id: "t" }, tenant: { id: "t", name: "Msasa", slug: "m", status: "active" } } },
      "GET /api/shifts/prefill/": { body: { stations: [station] } },
      "POST /api/sync/": (body) => ({ body: { results: body.ops.map((o: any) => ({ op_id: o.op_id, status: "applied" })) } }),
    });
    renderApp("/");
    const user = userEvent.setup();
    const meter = await screen.findByPlaceholderText("Meter number");
    expect(meter).toHaveValue("1000.00"); // prefilled from the last shift
    await user.click(screen.getByRole("button", { name: "Start shift" }));
    expect(await screen.findByRole("heading", { name: "End shift" })).toBeInTheDocument();

    await user.type(screen.getByPlaceholderText("Meter number now"), "1100");
    await user.type(screen.getByLabelText("Cash"), "150");
    await user.type(screen.getByLabelText("EcoCash / mobile money"), "50");
    // 100 litres x 2.00 = 200 expected, 200 handed in
    await waitFor(() => expect(screen.getByText("Expected").parentElement).toHaveTextContent("200.00"));
    expect(screen.getByText("Difference").parentElement).toHaveTextContent("0.00");
    await user.click(screen.getByRole("button", { name: "End shift" }));
    expect(await screen.findByRole("heading", { name: "Shift saved" })).toBeInTheDocument();

    await waitFor(() => expect(calls.filter((c) => c.path === "/api/sync/").length).toBeGreaterThan(0));
    const ops = calls.filter((c) => c.path === "/api/sync/").flatMap((c) => c.body.ops);
    expect(ops.map((o: any) => o.kind)).toEqual(["shift_start", "shift_end"]);
    expect(ops[1].payload.shift_id).toBe(ops[0].payload.shift_id);
    expect(ops[1].payload.cash).toEqual([{ payment_type: "cash", currency: "USD", amount: "150" }, { payment_type: "mobile_money", currency: "USD", amount: "50" }]);
    expect(await kvGet("activeShift")).toBeNull();
  });

  it("refuses to end until every started pump has an end number", async () => {
    signedIn(); localStorage.setItem("fs_tutorial_done", "1");
    mockApi({
      "GET /api/auth/me/": { body: { user: { id: "a", role: "attendant", full_name: "Tendai", must_change_password: false, tenant_id: "t" }, tenant: { id: "t", name: "Msasa", slug: "m", status: "active" } } },
      "GET /api/shifts/prefill/": { body: { stations: [station] } },
      "POST /api/sync/": { body: { results: [] } },
    });
    renderApp("/");
    const user = userEvent.setup();
    await screen.findByPlaceholderText("Meter number");
    await user.click(screen.getByRole("button", { name: "Start shift" }));
    await user.click(await screen.findByRole("button", { name: "End shift" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Enter the end number for 1 more pump");
  });

  it("works from the saved copy when the phone has no signal", async () => {
    signedIn(); localStorage.setItem("fs_tutorial_done", "1");
    const { fetchMock } = mockApi({ "GET /api/auth/me/": { body: { user: { id: "a", role: "attendant", full_name: "Tendai", must_change_password: false, tenant_id: "t" }, tenant: { id: "t", name: "Msasa", slug: "m", status: "active" } } } });
    const real = fetchMock.getMockImplementation()!;
    const { kvSet } = await import("../offline");
    await kvSet("prefill", [station]);
    fetchMock.mockImplementation(async (u: any, i?: RequestInit) => { if (String(u).includes("prefill")) throw new TypeError("offline"); return real(u, i); });
    renderApp("/");
    expect(await screen.findByPlaceholderText("Meter number")).toHaveValue("1000.00");
  });

  it("shows the first-run tutorial once", async () => {
    signedIn();
    mockApi({
      "GET /api/auth/me/": { body: { user: { id: "a", role: "attendant", full_name: "Tendai", must_change_password: false, tenant_id: "t" }, tenant: { id: "t", name: "Msasa", slug: "m", status: "active" } } },
      "GET /api/shifts/prefill/": { body: { stations: [station] } },
    });
    renderApp("/");
    const user = userEvent.setup();
    expect(await screen.findByRole("dialog", { name: "How it works" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Next" })); await user.click(screen.getByRole("button", { name: "Next" })); await user.click(screen.getByRole("button", { name: "Got it" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(localStorage.getItem("fs_tutorial_done")).toBe("1");
  });
});

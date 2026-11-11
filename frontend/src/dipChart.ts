/** A tank's dip chart as the phone gets it: [mm, litres] rows, lowest first. */
export type Chart = [string, string][];

/** The litres for a dip height, read off the chart the same way the server does (stations/services.litres_from_dip):
 * straight line between the two rows either side. "" when there is no chart, no number, or the height is off the chart. */
export function litresFromChart(chart: Chart | undefined, mmText: string): string {
  if (!chart || chart.length < 2 || !mmText.trim()) return "";
  const mm = Number(mmText.replace(",", "."));
  if (!Number.isFinite(mm)) return "";
  const rows = chart.map(([d, l]) => [Number(d), Number(l)]);
  for (let i = 1; i < rows.length; i++) {
    const [d0, l0] = rows[i - 1], [d1, l1] = rows[i];
    if (mm >= d0 && mm <= d1) {
      const litres = d1 === d0 ? l0 : l0 + ((l1 - l0) * (mm - d0)) / (d1 - d0);
      return String(Math.round(litres * 1000) / 1000);
    }
  }
  return "";
}
/** The tank's dip chart when it has a usable one (a phone with station data saved before charts were sent has none). */
export const chartOf = (tank: { calibration?: Chart } | undefined): Chart | undefined =>
  tank?.calibration && tank.calibration.length >= 2 ? tank.calibration : undefined;
/** A height typed for a tank with a chart, but outside it: the litres cannot be read off the chart. */
export const offChart = (tank: { calibration?: Chart } | undefined, mmText: string) =>
  !!chartOf(tank) && !!mmText.trim() && !litresFromChart(chartOf(tank), mmText);

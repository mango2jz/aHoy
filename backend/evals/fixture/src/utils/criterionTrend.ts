export type CriterionTrend = 'improving' | 'steady' | 'declining';

function mean(values: number[]): number {
    return values.reduce((sum, v) => sum + v, 0) / values.length;
}

/**
 * Classifies a per-criterion trend from oldest-to-newest band points.
 * Fewer than 2 points: no trend to read (`null`). 2-5 points: compares the
 * latest point against the mean of every earlier one. 6+ points: compares the
 * mean of the last 3 against the mean of the previous 3, smoothing single-
 * session noise. A delta at or past `threshold` in either direction is a move;
 * otherwise the criterion is steady.
 */
export function criterionTrend(points: number[], threshold = 0.5): CriterionTrend | null {
    if (points.length < 2) return null;

    let delta: number;
    if (points.length >= 6) {
        const last3 = points.slice(-3);
        const prev3 = points.slice(-6, -3);
        delta = mean(last3) - mean(prev3);
    } else {
        const last = points[points.length - 1];
        const rest = points.slice(0, -1);
        delta = last - mean(rest);
    }

    if (delta >= threshold) return 'improving';
    if (delta <= -threshold) return 'declining';
    return 'steady';
}

// Minimal vitest-compatible shim over node:test so the fixture runs with plain
// `node --test` and no npm install. Covers only what the vendored tests use.
import assert from 'node:assert/strict';
import {after, afterEach, before, beforeEach, describe as nDescribe, it as nIt, mock} from 'node:test';

export {afterEach, beforeEach, before, after};
export const describe = nDescribe;

function fmt(name: string, row: unknown): string {
    const args = Array.isArray(row) ? row : [row];
    let i = 0;
    return name.replace(/%[sdijo]/g, () => String(args[i++]));
}

type Fn = (...a: any[]) => unknown;
function each(table: readonly unknown[]) {
    return (name: string, fn: Fn) => {
        for (const row of table) nIt(fmt(name, row), () => fn(...(Array.isArray(row) ? row : [row])));
    };
}
export const it = Object.assign(nIt, {each});

class StringContaining {
    readonly needle: string;
    constructor(needle: string) {
        this.needle = needle;
    }
}

function eq(a: any, b: any): boolean {
    if (b instanceof StringContaining) return typeof a === 'string' && a.includes(b.needle);
    if (a === null || b === null || typeof a !== 'object' || typeof b !== 'object') return Object.is(a, b);
    if (Array.isArray(a) !== Array.isArray(b)) return false;
    const ka = Object.keys(a).filter((k) => a[k] !== undefined);
    const kb = Object.keys(b).filter((k) => b[k] !== undefined);
    return ka.length === kb.length && ka.every((k) => eq(a[k], b[k]));
}

class Matchers {
    private actual: any;
    private negate: boolean;
    constructor(actual: any, negate = false) {
        this.actual = actual;
        this.negate = negate;
    }
    get not() {
        return new Matchers(this.actual, !this.negate);
    }
    private check(ok: boolean, msg: string) {
        assert.ok(this.negate ? !ok : ok, (this.negate ? 'expected NOT: ' : 'expected: ') + msg);
    }
    toBe(exp: unknown) {
        this.check(Object.is(this.actual, exp), `${String(this.actual)} toBe ${String(exp)}`);
    }
    toEqual(exp: unknown) {
        this.check(eq(this.actual, exp), `${JSON.stringify(this.actual)} toEqual ${JSON.stringify(exp)}`);
    }
    toBeNull() {
        this.check(this.actual === null, `${String(this.actual)} toBeNull`);
    }
    toBeCloseTo(exp: number, digits = 2) {
        this.check(Math.abs(this.actual - exp) < 10 ** -digits / 2, `${this.actual} toBeCloseTo ${exp}`);
    }
    toContain(item: unknown) {
        this.check(this.actual.includes(item), `${String(this.actual)} toContain ${String(item)}`);
    }
    toHaveBeenCalled() {
        this.check(this.actual.mock.callCount() > 0, 'toHaveBeenCalled');
    }
    toHaveBeenCalledOnce() {
        this.check(this.actual.mock.callCount() === 1, 'toHaveBeenCalledOnce');
    }
    toHaveBeenCalledTimes(n: number) {
        this.check(this.actual.mock.callCount() === n, `toHaveBeenCalledTimes ${n}`);
    }
}
export const expect = Object.assign((actual: unknown) => new Matchers(actual), {
    stringContaining: (needle: string) => new StringContaining(needle),
});

export const vi = {fn: (impl?: Fn) => mock.fn(impl)};

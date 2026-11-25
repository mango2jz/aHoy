/**
 * Parses QUESTIONS_FOR_WEBSITE_DEVELOPMENT.md into rows matching the `scenarios` DB table.
 *
 * Source format: Markdown table with 4 columns
 *   | Unit | Question | Hints for speaking | Suggested vocab |
 *
 * Output matches `scenarios` Insert shape:
 *   { id, unit_id, situation, question, options, follow_up_question, suggested_vocab, hints, min_duration_seconds }
 *
 * options: [{label, pros, cons}] — label from Question cell, pros/cons from Hints cell
 * hints:   {solution_1: {pros, cons}, solution_2: ..., solution_3: ..., alternatives: string[]}
 *
 * Fragile by design — tied to this specific file's formatting conventions.
 * If source format changes, update the sub-parsers, not the caller.
 */

/**
 * Speaking floor per scenario, per the source doc's "2 minutes". Counts
 * cumulative student speaking time, not wall clock, so an attempt runs roughly
 * twice this long end to end.
 */
const MIN_DURATION_SECONDS = 120;

export interface ScenarioInsert {
    id: string;
    unit_id: string;
    situation: string;
    question: string;
    options: ScenarioOption[];
    follow_up_question: string | null;
    suggested_vocab: string[];
    min_duration_seconds: number;
    hints: ScenarioHints;
}

export interface ScenarioOption {
    label: string;
    pros: string;
    cons: string;
}

export interface ScenarioHints {
    solution_1: { pros: string; cons: string };
    solution_2: { pros: string; cons: string };
    solution_3: { pros: string; cons: string };
    alternatives: string[];
}

export function parseScenarioMarkdown(rawMarkdown: string): ScenarioInsert[] {
    const scenarios: ScenarioInsert[] = [];

    const lines = rawMarkdown.split('\n');
    const tableRows = lines.filter(
        (line) =>
            line.startsWith('|') &&
            !line.match(/^\|\s*:?-+:?\s*\|/) &&
            !line.includes('Unit') &&
            // Skip the rubric/scoring table at the bottom
            !line.includes('Score') &&
            !line.includes('Descriptor')
    );

    for (const row of tableRows) {
        const cells = splitMarkdownTableRow(row);
        if (cells.length < 4) continue;

        const [unitCell, questionCell, hintsCell, vocabCell] = cells;

        const unit_id = parseUnitId(unitCell);
        const { situation, question, optionLabels, follow_up_question, min_duration_seconds } =
            parseQuestionCell(questionCell);
        const { solutions, alternatives } = parseHintsCell(hintsCell);
        const suggested_vocab = parseVocabCell(vocabCell);

        // Merge option labels (from Question cell) with pros/cons (from Hints cell)
        const options: ScenarioOption[] = optionLabels.map((label, i) => ({
            label,
            pros: solutions[i]?.pros ?? '',
            cons: solutions[i]?.cons ?? '',
        }));

        const hints: ScenarioHints = {
            solution_1: solutions[0] ?? { pros: '', cons: '' },
            solution_2: solutions[1] ?? { pros: '', cons: '' },
            solution_3: solutions[2] ?? { pros: '', cons: '' },
            alternatives,
        };

        scenarios.push({
            id: generateScenarioId(unit_id, situation),
            unit_id,
            situation,
            question,
            options,
            follow_up_question,
            suggested_vocab,
            min_duration_seconds,
            hints,
        });
    }

    return scenarios;
}

function splitMarkdownTableRow(row: string): string[] {
    const trimmed = row.replace(/^\||\|$/g, '');
    return trimmed.split('|').map((cell) => cell.trim());
}

/**
 * Maps display unit names to FK-safe unit IDs matching the `units` table.
 * "Travel 1" → "travel-1", "Brand" → "brand", etc.
 */
function parseUnitId(cell: string): string {
    const raw = cell.replace(/\*+/g, '').trim();
    return raw
        .toLowerCase()
        .replace(/\s+/g, '-')
        .replace(/[^a-z0-9-]/g, '');
}

function parseQuestionCell(cell: string): {
    situation: string;
    question: string;
    optionLabels: string[];
    follow_up_question: string | null;
    min_duration_seconds: number;
} {
    const clean = cell.replace(/\*+/g, '');

    const situationMatch = clean.match(/Situation:\s*([\s\S]*?)Question:/i);
    const questionMatch = clean.match(/Question:\s*([\s\S]*?)(?:-{3,}|Follow-up|$)/i);
    const followUpMatch = clean.match(/Follow-up question:\s*([\s\S]*?)$/i);

    const situation = situationMatch?.[1]?.trim() ?? '';
    const questionBlock = questionMatch?.[1]?.trim() ?? '';
    const follow_up_question = followUpMatch?.[1]?.trim() || null;

    // First sentence/clause up to "?" = the question; remainder = option labels
    const sentences = questionBlock.split(/(?<=[?.:])\s+(?=[A-Z])/);
    const question = sentences[0]?.trim() ?? '';
    const optionLabels = sentences
        .slice(1)
        .map((s) => s.replace(/^You should speak.*$/im, '').trim())
        .filter((s) => s.length > 10);

    return { situation, question, optionLabels, follow_up_question, min_duration_seconds: MIN_DURATION_SECONDS };
}

function parseHintsCell(cell: string): {
    solutions: Array<{ pros: string; cons: string }>;
    alternatives: string[];
} {
    const clean = cell.replace(/\*+/g, '');

    const splitMarker = /----+\/*-*|Other (approaches|solutions):/i;
    const parts = clean.split(splitMarker);
    const solutionsSection = parts[0] ?? '';
    const alternativesSection = parts[parts.length - 1] ?? '';

    const solutionBlocks = solutionsSection.split(/SOLUTION\s+\d+:?/i).slice(1);
    const solutions = solutionBlocks.map((block) => {
        const prosMatch = block.match(/Pros\s*\(?\+?\)?:?\s*([\s\S]*?)(?:Cons|\n\n|$)/i);
        const consMatch = block.match(/Cons\s*\(?-?\)?:?\s*([\s\S]*?)(?:SOLUTION|\n\n|$)/i);
        return {
            pros: prosMatch?.[1]?.trim() ?? '',
            cons: consMatch?.[1]?.trim() ?? '',
        };
    });

    const alternatives = alternativesSection
        .split(/\n|(?<=[.])\s+(?=[A-Z])/)
        .map((line) => line.replace(/^[-•*]\s*/, '').trim())
        .filter((line) => line.length > 10);

    return { solutions, alternatives };
}

function parseVocabCell(cell: string): string[] {
    return cell
        .replace(/\*+/g, '')
        .split(/[,\n]/)
        .map((term) => term.trim())
        .filter((term) => term.length > 0 && term.length < 100);
}

/**
 * Stable upsert-safe ID. Same unit + situation → same ID on every parse run.
 */
function generateScenarioId(unit_id: string, situation: string): string {
    const slug = (unit_id + '-' + situation.slice(0, 40))
        .toLowerCase()
        .replace(/[^a-z0-9]+/g, '-')
        .replace(/^-|-$/g, '');
    return slug;
}
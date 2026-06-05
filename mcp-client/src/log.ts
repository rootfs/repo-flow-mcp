/** Tiny stderr logger that timestamps lines. Used everywhere instead of console.log. */
export function makeLog(prefix: string): (line: string) => void {
    return (line: string) => process.stderr.write(`[${prefix}] ${line}\n`);
}

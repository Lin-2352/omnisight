// Contract versions are "major.minor.patch"; nodes older than a feature must not be sent its fields.

export function parseVersion(version: string | undefined | null): [number, number, number] {
  const match = /^(\d+)\.(\d+)\.(\d+)/.exec(version ?? "");
  return match ? [Number(match[1]), Number(match[2]), Number(match[3])] : [0, 0, 0];
}

export function versionAtLeast(version: string | undefined | null, minimum: string): boolean {
  const have = parseVersion(version);
  const need = parseVersion(minimum);
  for (let index = 0; index < 3; index += 1) {
    if ((have[index] ?? 0) !== (need[index] ?? 0)) return (have[index] ?? 0) > (need[index] ?? 0);
  }
  return true;
}

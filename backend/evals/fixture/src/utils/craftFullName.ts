export default function craftFullName(
    firstName: string | null | undefined,
    lastName: string | null | undefined
): string {
    return `${firstName} ${lastName}`.trim() || 'Unknown';
}

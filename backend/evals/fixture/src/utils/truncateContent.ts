/**
 * Truncates the given content to a specified maximum length.
 * @param content - The content string to truncate.
 * @param maxLength - The maximum length of the truncated content. Default is 100.
 * @returns The truncated content with an ellipsis if it exceeds the maximum length, or 'No description' if content is null.
 * */
export default function truncateContent(content: string | null, maxLength = 100) {
    if (!content) return 'No description';
    if (content.length <= maxLength) return content;
    return content.substring(0, maxLength).trim() + '...';
}

import { useState } from "react";

export function releaseAttachmentPreviews(items) {
  items.forEach((item) => {
    if (item.preview) URL.revokeObjectURL(item.preview);
  });
}

export default function useAttachments(showToast) {
  const [attachments, setAttachments] = useState([]);

  function addAttachments(fileList) {
    const selected = Array.from(fileList || []);
    const valid = [];
    for (const file of selected) {
      if (file.size > 10 * 1024 * 1024) {
        showToast(`${file.name} 超过 10 MB`);
        continue;
      }
      valid.push({
        file,
        preview: file.type.startsWith("image/") ? URL.createObjectURL(file) : "",
      });
    }
    setAttachments((current) => {
      const slots = Math.max(0, 5 - current.length);
      if (valid.length > slots) showToast("每次最多上传 5 个文件");
      releaseAttachmentPreviews(valid.slice(slots));
      return [...current, ...valid.slice(0, slots)];
    });
  }

  function removeAttachment(index) {
    setAttachments((current) => {
      releaseAttachmentPreviews(current.slice(index, index + 1));
      return current.filter((_, itemIndex) => itemIndex !== index);
    });
  }

  return {
    attachments,
    addAttachments,
    clearAttachments: () => setAttachments([]),
    removeAttachment,
  };
}

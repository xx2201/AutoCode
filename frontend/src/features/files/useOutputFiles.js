import { useState } from "react";

export default function useOutputFiles({
  token,
  workspace,
  clientId,
  showToast,
  onUnauthorized,
}) {
  const [downloadingFileId, setDownloadingFileId] = useState("");

  async function handleOutputFile(file, openInBrowser) {
    if (!workspace || downloadingFileId) return;
    const previewWindow = openInBrowser ? window.open("", "_blank") : null;
    if (openInBrowser && !previewWindow) {
      showToast("浏览器阻止了新页面，请允许弹窗后重试。");
      return;
    }
    setDownloadingFileId(file.file_id);
    try {
      const response = await fetch("/api/download", {
        method: "POST",
        headers: {
          Authorization: `Bearer ${token}`,
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          client_id: clientId,
          workspace_id: workspace.workspace_id,
          file_id: file.file_id,
        }),
      });
      if (!response.ok) {
        const data = await response.json().catch(() => ({}));
        const error = new Error(data.detail || `文件获取失败 (${response.status})`);
        error.status = response.status;
        throw error;
      }
      const url = URL.createObjectURL(await response.blob());
      if (previewWindow) {
        previewWindow.location.href = url;
      } else {
        const link = document.createElement("a");
        link.href = url;
        link.download = file.name;
        document.body.appendChild(link);
        link.click();
        link.remove();
      }
      window.setTimeout(() => URL.revokeObjectURL(url), 60_000);
    } catch (error) {
      previewWindow?.close();
      showToast(error.message);
      if (error.status === 401) onUnauthorized();
    } finally {
      setDownloadingFileId("");
    }
  }

  return { downloadingFileId, handleOutputFile };
}

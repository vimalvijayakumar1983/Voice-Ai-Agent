'use client';

import { useState, useCallback, useRef } from 'react';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import {
  Upload,
  FileText,
  X,
  CheckCircle,
  AlertCircle,
  Loader2,
  File,
} from 'lucide-react';
import { cn } from '@/lib/utils';

// ---------- Types ----------

interface UploadFile {
  id: string;
  file: File;
  status: 'pending' | 'uploading' | 'processing' | 'complete' | 'error';
  progress: number;
  error?: string;
  documentId?: string;
}

interface DocumentUploadProps {
  knowledgeBaseId: string;
  onUploadComplete?: (documentId: string, fileName: string) => void;
  onError?: (fileName: string, error: string) => void;
  maxFiles?: number;
  maxFileSize?: number; // in bytes
  className?: string;
}

const ACCEPTED_TYPES: Record<string, string> = {
  'application/pdf': 'PDF',
  'application/vnd.openxmlformats-officedocument.wordprocessingml.document': 'DOCX',
  'text/plain': 'TXT',
  'text/csv': 'CSV',
  'text/html': 'HTML',
  'text/markdown': 'Markdown',
};

const ACCEPTED_EXTENSIONS = '.pdf,.docx,.txt,.csv,.html,.md';

const DEFAULT_MAX_SIZE = 50 * 1024 * 1024; // 50MB

// ---------- Component ----------

export function DocumentUpload({
  knowledgeBaseId,
  onUploadComplete,
  onError,
  maxFiles = 10,
  maxFileSize = DEFAULT_MAX_SIZE,
  className,
}: DocumentUploadProps) {
  const [files, setFiles] = useState<UploadFile[]>([]);
  const [isDragOver, setIsDragOver] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  const validateFile = useCallback(
    (file: File): string | null => {
      if (!Object.keys(ACCEPTED_TYPES).includes(file.type)) {
        // Also check by extension for markdown
        const ext = file.name.split('.').pop()?.toLowerCase();
        if (ext !== 'md' && ext !== 'markdown') {
          return `Unsupported file type: ${file.type || file.name.split('.').pop()}`;
        }
      }

      if (file.size > maxFileSize) {
        return `File too large: ${(file.size / 1024 / 1024).toFixed(1)}MB (max ${(maxFileSize / 1024 / 1024).toFixed(0)}MB)`;
      }

      return null;
    },
    [maxFileSize],
  );

  const addFiles = useCallback(
    (newFiles: FileList | File[]) => {
      const filesToAdd: UploadFile[] = [];

      for (const file of Array.from(newFiles)) {
        if (files.length + filesToAdd.length >= maxFiles) {
          onError?.(file.name, `Maximum ${maxFiles} files allowed`);
          break;
        }

        const error = validateFile(file);
        if (error) {
          onError?.(file.name, error);
          continue;
        }

        filesToAdd.push({
          id: `${Date.now()}-${Math.random().toString(36).substring(2)}`,
          file,
          status: 'pending',
          progress: 0,
        });
      }

      if (filesToAdd.length > 0) {
        setFiles((prev) => [...prev, ...filesToAdd]);
      }
    },
    [files.length, maxFiles, validateFile, onError],
  );

  const removeFile = useCallback((fileId: string) => {
    setFiles((prev) => prev.filter((f) => f.id !== fileId));
  }, []);

  const handleDragOver = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    setIsDragOver(true);
  }, []);

  const handleDragLeave = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    setIsDragOver(false);
  }, []);

  const handleDrop = useCallback(
    (e: React.DragEvent) => {
      e.preventDefault();
      setIsDragOver(false);
      if (e.dataTransfer.files) {
        addFiles(e.dataTransfer.files);
      }
    },
    [addFiles],
  );

  const handleFileSelect = useCallback(
    (e: React.ChangeEvent<HTMLInputElement>) => {
      if (e.target.files) {
        addFiles(e.target.files);
      }
      // Reset input
      if (inputRef.current) inputRef.current.value = '';
    },
    [addFiles],
  );

  const uploadFile = useCallback(
    async (uploadFile: UploadFile) => {
      setFiles((prev) =>
        prev.map((f) =>
          f.id === uploadFile.id ? { ...f, status: 'uploading' as const, progress: 0 } : f,
        ),
      );

      try {
        // 1. Get presigned upload URL
        const presignResponse = await fetch(
          `${process.env.NEXT_PUBLIC_API_URL}/knowledge-bases/${knowledgeBaseId}/upload-url`,
          {
            method: 'POST',
            headers: {
              'Content-Type': 'application/json',
              Authorization: `Bearer ${getStoredToken()}`,
            },
            body: JSON.stringify({
              fileName: uploadFile.file.name,
              contentType: uploadFile.file.type || 'application/octet-stream',
              fileSize: uploadFile.file.size,
            }),
          },
        );

        if (!presignResponse.ok) {
          throw new Error('Failed to get upload URL');
        }

        const { uploadUrl, fileKey } = await presignResponse.json();

        // 2. Upload file to S3
        const uploadResponse = await new Promise<void>((resolve, reject) => {
          const xhr = new XMLHttpRequest();
          xhr.open('PUT', uploadUrl);
          xhr.setRequestHeader('Content-Type', uploadFile.file.type || 'application/octet-stream');

          xhr.upload.onprogress = (e) => {
            if (e.lengthComputable) {
              const progress = Math.round((e.loaded / e.total) * 100);
              setFiles((prev) =>
                prev.map((f) => (f.id === uploadFile.id ? { ...f, progress } : f)),
              );
            }
          };

          xhr.onload = () => {
            if (xhr.status >= 200 && xhr.status < 300) resolve();
            else reject(new Error(`Upload failed: ${xhr.status}`));
          };

          xhr.onerror = () => reject(new Error('Upload network error'));
          xhr.send(uploadFile.file);
        });

        // 3. Register document for processing
        setFiles((prev) =>
          prev.map((f) =>
            f.id === uploadFile.id ? { ...f, status: 'processing' as const, progress: 100 } : f,
          ),
        );

        const docResponse = await fetch(
          `${process.env.NEXT_PUBLIC_API_URL}/knowledge-bases/${knowledgeBaseId}/documents`,
          {
            method: 'POST',
            headers: {
              'Content-Type': 'application/json',
              Authorization: `Bearer ${getStoredToken()}`,
            },
            body: JSON.stringify({
              fileName: uploadFile.file.name,
              contentType: uploadFile.file.type || 'application/octet-stream',
              fileKey,
            }),
          },
        );

        if (!docResponse.ok) {
          throw new Error('Failed to register document');
        }

        const document = await docResponse.json();

        setFiles((prev) =>
          prev.map((f) =>
            f.id === uploadFile.id
              ? { ...f, status: 'complete' as const, documentId: document.id }
              : f,
          ),
        );

        onUploadComplete?.(document.id, uploadFile.file.name);
      } catch (error) {
        const errorMessage = (error as Error).message;

        setFiles((prev) =>
          prev.map((f) =>
            f.id === uploadFile.id ? { ...f, status: 'error' as const, error: errorMessage } : f,
          ),
        );

        onError?.(uploadFile.file.name, errorMessage);
      }
    },
    [knowledgeBaseId, onUploadComplete, onError],
  );

  const uploadAll = useCallback(async () => {
    const pendingFiles = files.filter((f) => f.status === 'pending');
    for (const file of pendingFiles) {
      await uploadFile(file);
    }
  }, [files, uploadFile]);

  const pendingCount = files.filter((f) => f.status === 'pending').length;
  const uploadingCount = files.filter(
    (f) => f.status === 'uploading' || f.status === 'processing',
  ).length;

  return (
    <Card className={className}>
      <CardHeader>
        <CardTitle className="text-lg flex items-center gap-2">
          <Upload className="h-5 w-5" />
          Upload Documents
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        {/* Drop Zone */}
        <div
          className={cn(
            'border-2 border-dashed rounded-lg p-8 text-center transition-colors cursor-pointer',
            isDragOver
              ? 'border-primary bg-primary/5'
              : 'border-muted-foreground/25 hover:border-primary/50',
          )}
          onDragOver={handleDragOver}
          onDragLeave={handleDragLeave}
          onDrop={handleDrop}
          onClick={() => inputRef.current?.click()}
        >
          <Upload className="h-10 w-10 mx-auto text-muted-foreground mb-3" />
          <p className="text-sm font-medium">
            Drag and drop files here, or click to browse
          </p>
          <p className="text-xs text-muted-foreground mt-1">
            PDF, DOCX, TXT, CSV, HTML, Markdown (max{' '}
            {(maxFileSize / 1024 / 1024).toFixed(0)}MB each)
          </p>
          <input
            ref={inputRef}
            type="file"
            accept={ACCEPTED_EXTENSIONS}
            multiple
            className="hidden"
            onChange={handleFileSelect}
          />
        </div>

        {/* File List */}
        {files.length > 0 && (
          <div className="space-y-2">
            {files.map((file) => (
              <FileItem
                key={file.id}
                file={file}
                onRemove={() => removeFile(file.id)}
                onRetry={() => uploadFile(file)}
              />
            ))}
          </div>
        )}

        {/* Actions */}
        {pendingCount > 0 && (
          <div className="flex items-center justify-between">
            <p className="text-sm text-muted-foreground">
              {pendingCount} file{pendingCount !== 1 ? 's' : ''} ready to upload
            </p>
            <Button onClick={uploadAll} disabled={uploadingCount > 0}>
              {uploadingCount > 0 ? (
                <>
                  <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                  Uploading...
                </>
              ) : (
                <>
                  <Upload className="mr-2 h-4 w-4" />
                  Upload All
                </>
              )}
            </Button>
          </div>
        )}
      </CardContent>
    </Card>
  );
}

// ---------- File Item ----------

function FileItem({
  file,
  onRemove,
  onRetry,
}: {
  file: UploadFile;
  onRemove: () => void;
  onRetry: () => void;
}) {
  const ext = file.file.name.split('.').pop()?.toUpperCase() || 'FILE';
  const size = formatFileSize(file.file.size);

  return (
    <div className="flex items-center gap-3 rounded-lg border p-3">
      <div className="flex-shrink-0">
        <FileIcon extension={ext} />
      </div>

      <div className="flex-1 min-w-0">
        <p className="text-sm font-medium truncate">{file.file.name}</p>
        <div className="flex items-center gap-2 mt-0.5">
          <span className="text-xs text-muted-foreground">{size}</span>
          <StatusBadge status={file.status} />
        </div>

        {/* Progress bar */}
        {(file.status === 'uploading' || file.status === 'processing') && (
          <div className="mt-2 h-1.5 w-full bg-muted rounded-full overflow-hidden">
            <div
              className={cn(
                'h-full rounded-full transition-all duration-300',
                file.status === 'processing'
                  ? 'bg-yellow-500 animate-pulse'
                  : 'bg-primary',
              )}
              style={{ width: `${file.progress}%` }}
            />
          </div>
        )}

        {file.error && (
          <p className="text-xs text-destructive mt-1">{file.error}</p>
        )}
      </div>

      <div className="flex-shrink-0 flex items-center gap-1">
        {file.status === 'error' && (
          <Button variant="ghost" size="icon" className="h-7 w-7" onClick={onRetry}>
            <Loader2 className="h-3.5 w-3.5" />
          </Button>
        )}
        {file.status !== 'uploading' && file.status !== 'processing' && (
          <Button variant="ghost" size="icon" className="h-7 w-7" onClick={onRemove}>
            <X className="h-3.5 w-3.5" />
          </Button>
        )}
      </div>
    </div>
  );
}

// ---------- Sub-components ----------

function FileIcon({ extension }: { extension: string }) {
  const colors: Record<string, string> = {
    PDF: 'bg-red-100 text-red-700 dark:bg-red-900/30 dark:text-red-400',
    DOCX: 'bg-blue-100 text-blue-700 dark:bg-blue-900/30 dark:text-blue-400',
    TXT: 'bg-gray-100 text-gray-700 dark:bg-gray-900/30 dark:text-gray-400',
    CSV: 'bg-green-100 text-green-700 dark:bg-green-900/30 dark:text-green-400',
    HTML: 'bg-orange-100 text-orange-700 dark:bg-orange-900/30 dark:text-orange-400',
    MD: 'bg-purple-100 text-purple-700 dark:bg-purple-900/30 dark:text-purple-400',
  };

  return (
    <div
      className={cn(
        'h-10 w-10 rounded-lg flex items-center justify-center',
        colors[extension] || 'bg-muted text-muted-foreground',
      )}
    >
      <FileText className="h-5 w-5" />
    </div>
  );
}

function StatusBadge({ status }: { status: UploadFile['status'] }) {
  switch (status) {
    case 'pending':
      return <Badge variant="secondary" className="text-[10px]">Pending</Badge>;
    case 'uploading':
      return <Badge variant="default" className="text-[10px]">Uploading</Badge>;
    case 'processing':
      return <Badge variant="warning" className="text-[10px]">Processing</Badge>;
    case 'complete':
      return (
        <Badge variant="success" className="text-[10px]">
          <CheckCircle className="h-3 w-3 mr-1" />
          Complete
        </Badge>
      );
    case 'error':
      return (
        <Badge variant="destructive" className="text-[10px]">
          <AlertCircle className="h-3 w-3 mr-1" />
          Error
        </Badge>
      );
    default:
      return null;
  }
}

// ---------- Helpers ----------

function formatFileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function getStoredToken(): string {
  if (typeof window === 'undefined') return '';
  return localStorage.getItem('auth_token') || '';
}

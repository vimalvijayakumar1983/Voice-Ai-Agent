'use client';

import { useCallback, useState, useRef } from 'react';
import { useApiQuery, useApiMutation } from './use-api';
import { useQueryClient } from '@tanstack/react-query';
import api from '@/lib/api';

// ---------- Types ----------

interface KnowledgeBase {
  id: string;
  name: string;
  description?: string;
  type: string;
  tenantId: string;
  metadata: Record<string, unknown>;
  createdAt: string;
  updatedAt: string;
  _count?: { documents: number };
}

interface KBDocument {
  id: string;
  knowledgeBaseId: string;
  fileName: string;
  contentType: string;
  fileKey: string;
  status: string;
  tenantId: string;
  metadata: Record<string, unknown>;
  createdAt: string;
  updatedAt: string;
}

interface PaginatedResponse<T> {
  data: T[];
  meta: {
    page: number;
    limit: number;
    totalItems: number;
    totalPages: number;
    hasNextPage: boolean;
    hasPreviousPage: boolean;
  };
}

interface SearchResult {
  id: string;
  content: string;
  score: number;
  documentId: string;
  knowledgeBaseId: string;
  chunkIndex: number;
  metadata: Record<string, unknown>;
}

interface RAGResponse {
  answer: string;
  sources: SearchResult[];
  confidence: number;
  tokensUsed: { prompt: number; completion: number; total: number };
  retrievedChunks: number;
  searchStrategy: string;
  cached: boolean;
}

interface CreateKnowledgeBaseDto {
  name: string;
  description?: string;
  type?: string;
  metadata?: Record<string, unknown>;
}

interface UploadProgress {
  fileName: string;
  progress: number;
  status: 'uploading' | 'processing' | 'complete' | 'error';
  error?: string;
  documentId?: string;
}

// ---------- Knowledge Base CRUD Hooks ----------

export function useKnowledgeBases(page = 1, limit = 20) {
  return useApiQuery<PaginatedResponse<KnowledgeBase>>(
    ['knowledge-bases', String(page), String(limit)],
    `/knowledge-bases?page=${page}&limit=${limit}`,
  );
}

export function useKnowledgeBase(id: string) {
  return useApiQuery<KnowledgeBase & { documents: KBDocument[] }>(
    ['knowledge-bases', id],
    `/knowledge-bases/${id}`,
    { enabled: !!id },
  );
}

export function useCreateKnowledgeBase() {
  return useApiMutation<KnowledgeBase, CreateKnowledgeBaseDto>(
    '/knowledge-bases',
    'post',
    [['knowledge-bases']],
  );
}

export function useUpdateKnowledgeBase(id: string) {
  return useApiMutation<KnowledgeBase, Partial<CreateKnowledgeBaseDto>>(
    `/knowledge-bases/${id}`,
    'put',
    [['knowledge-bases'], ['knowledge-bases', id]],
  );
}

export function useDeleteKnowledgeBase() {
  const queryClient = useQueryClient();

  return useApiMutation<void, string>(
    '', // URL is dynamic
    'delete',
    [['knowledge-bases']],
  );
}

// ---------- Document Upload with Progress ----------

export function useDocumentUpload(knowledgeBaseId: string) {
  const [uploads, setUploads] = useState<Map<string, UploadProgress>>(new Map());
  const queryClient = useQueryClient();
  const abortControllers = useRef<Map<string, AbortController>>(new Map());

  const upload = useCallback(
    async (file: File): Promise<string | null> => {
      const uploadId = `${Date.now()}-${Math.random().toString(36).substring(2)}`;

      setUploads((prev) => {
        const next = new Map(prev);
        next.set(uploadId, {
          fileName: file.name,
          progress: 0,
          status: 'uploading',
        });
        return next;
      });

      const controller = new AbortController();
      abortControllers.current.set(uploadId, controller);

      try {
        // 1. Get presigned URL
        const { data: presign } = await api.post(
          `/knowledge-bases/${knowledgeBaseId}/upload-url`,
          {
            fileName: file.name,
            contentType: file.type || 'application/octet-stream',
            fileSize: file.size,
          },
        );

        // 2. Upload to S3 with progress tracking
        await new Promise<void>((resolve, reject) => {
          const xhr = new XMLHttpRequest();
          xhr.open('PUT', presign.uploadUrl);
          xhr.setRequestHeader('Content-Type', file.type || 'application/octet-stream');

          xhr.upload.onprogress = (e) => {
            if (e.lengthComputable) {
              const progress = Math.round((e.loaded / e.total) * 100);
              setUploads((prev) => {
                const next = new Map(prev);
                const current = next.get(uploadId);
                if (current) next.set(uploadId, { ...current, progress });
                return next;
              });
            }
          };

          xhr.onload = () => {
            if (xhr.status >= 200 && xhr.status < 300) resolve();
            else reject(new Error(`Upload failed: ${xhr.status}`));
          };

          xhr.onerror = () => reject(new Error('Network error'));
          xhr.onabort = () => reject(new Error('Upload aborted'));

          controller.signal.addEventListener('abort', () => xhr.abort());
          xhr.send(file);
        });

        // 3. Register document
        setUploads((prev) => {
          const next = new Map(prev);
          const current = next.get(uploadId);
          if (current) next.set(uploadId, { ...current, status: 'processing', progress: 100 });
          return next;
        });

        const { data: document } = await api.post(
          `/knowledge-bases/${knowledgeBaseId}/documents`,
          {
            fileName: file.name,
            contentType: file.type || 'application/octet-stream',
            fileKey: presign.fileKey,
          },
        );

        setUploads((prev) => {
          const next = new Map(prev);
          const current = next.get(uploadId);
          if (current) {
            next.set(uploadId, {
              ...current,
              status: 'complete',
              documentId: document.id,
            });
          }
          return next;
        });

        // Invalidate queries
        queryClient.invalidateQueries({ queryKey: ['knowledge-bases', knowledgeBaseId] });

        return document.id;
      } catch (error) {
        setUploads((prev) => {
          const next = new Map(prev);
          const current = next.get(uploadId);
          if (current) {
            next.set(uploadId, {
              ...current,
              status: 'error',
              error: (error as Error).message,
            });
          }
          return next;
        });

        return null;
      } finally {
        abortControllers.current.delete(uploadId);
      }
    },
    [knowledgeBaseId, queryClient],
  );

  const cancelUpload = useCallback((uploadId: string) => {
    const controller = abortControllers.current.get(uploadId);
    if (controller) {
      controller.abort();
      abortControllers.current.delete(uploadId);
    }
  }, []);

  const clearCompleted = useCallback(() => {
    setUploads((prev) => {
      const next = new Map(prev);
      for (const [id, upload] of next) {
        if (upload.status === 'complete' || upload.status === 'error') {
          next.delete(id);
        }
      }
      return next;
    });
  }, []);

  return {
    uploads: Array.from(uploads.entries()).map(([id, data]) => ({ id, ...data })),
    upload,
    cancelUpload,
    clearCompleted,
    isUploading: Array.from(uploads.values()).some(
      (u) => u.status === 'uploading' || u.status === 'processing',
    ),
  };
}

// ---------- Search with Debouncing ----------

export function useKBSearch(options?: {
  knowledgeBaseId?: string;
  knowledgeBaseIds?: string[];
  debounceMs?: number;
}) {
  const [results, setResults] = useState<SearchResult[]>([]);
  const [answer, setAnswer] = useState<string | null>(null);
  const [confidence, setConfidence] = useState(0);
  const [isSearching, setIsSearching] = useState(false);
  const debounceRef = useRef<NodeJS.Timeout>();

  const search = useCallback(
    async (query: string) => {
      if (!query.trim()) {
        setResults([]);
        setAnswer(null);
        setConfidence(0);
        return;
      }

      setIsSearching(true);

      try {
        const body: Record<string, unknown> = {
          query,
          topK: 8,
          searchStrategy: 'hybrid',
          includeSourceAttribution: true,
        };

        if (options?.knowledgeBaseId) body.knowledgeBaseId = options.knowledgeBaseId;
        if (options?.knowledgeBaseIds) body.knowledgeBaseIds = options.knowledgeBaseIds;

        const { data } = await api.post<RAGResponse>(
          '/knowledge-bases/rag/query',
          body,
        );

        setResults(data.sources);
        setAnswer(data.answer);
        setConfidence(data.confidence);
      } catch (error) {
        console.error('Search failed:', error);
        setResults([]);
        setAnswer(null);
      } finally {
        setIsSearching(false);
      }
    },
    [options?.knowledgeBaseId, options?.knowledgeBaseIds],
  );

  const debouncedSearch = useCallback(
    (query: string) => {
      if (debounceRef.current) clearTimeout(debounceRef.current);

      debounceRef.current = setTimeout(() => {
        search(query);
      }, options?.debounceMs ?? 400);
    },
    [search, options?.debounceMs],
  );

  const clearResults = useCallback(() => {
    setResults([]);
    setAnswer(null);
    setConfidence(0);
  }, []);

  return {
    results,
    answer,
    confidence,
    isSearching,
    search,
    debouncedSearch,
    clearResults,
  };
}

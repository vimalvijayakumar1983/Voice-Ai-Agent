'use client';

import { useState, useCallback, useRef, useEffect } from 'react';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import {
  Search,
  FileText,
  ChevronDown,
  ChevronUp,
  ExternalLink,
  Loader2,
  Sparkles,
  BookOpen,
} from 'lucide-react';
import { cn } from '@/lib/utils';

// ---------- Types ----------

interface KBSearchProps {
  knowledgeBaseId?: string;
  knowledgeBaseIds?: string[];
  placeholder?: string;
  onResultSelect?: (result: SearchResult) => void;
  showAnswer?: boolean;
  className?: string;
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

// ---------- Component ----------

export function KBSearch({
  knowledgeBaseId,
  knowledgeBaseIds,
  placeholder = 'Search knowledge base...',
  onResultSelect,
  showAnswer = true,
  className,
}: KBSearchProps) {
  const [query, setQuery] = useState('');
  const [results, setResults] = useState<SearchResult[]>([]);
  const [answer, setAnswer] = useState<string | null>(null);
  const [confidence, setConfidence] = useState<number>(0);
  const [isSearching, setIsSearching] = useState(false);
  const [expandedChunks, setExpandedChunks] = useState<Set<string>>(new Set());
  const [searchMeta, setSearchMeta] = useState<{
    strategy: string;
    chunks: number;
    cached: boolean;
  } | null>(null);
  const debounceRef = useRef<NodeJS.Timeout>();

  const handleSearch = useCallback(
    async (searchQuery: string) => {
      if (!searchQuery.trim()) {
        setResults([]);
        setAnswer(null);
        setSearchMeta(null);
        return;
      }

      setIsSearching(true);

      try {
        const body: Record<string, unknown> = {
          query: searchQuery,
          topK: 8,
          searchStrategy: 'hybrid',
          includeSourceAttribution: true,
        };

        if (knowledgeBaseId) body.knowledgeBaseId = knowledgeBaseId;
        if (knowledgeBaseIds) body.knowledgeBaseIds = knowledgeBaseIds;

        const response = await fetch(
          `${process.env.NEXT_PUBLIC_API_URL}/knowledge-bases/rag/query`,
          {
            method: 'POST',
            headers: {
              'Content-Type': 'application/json',
              Authorization: `Bearer ${getStoredToken()}`,
            },
            body: JSON.stringify(body),
          },
        );

        if (!response.ok) {
          throw new Error('Search failed');
        }

        const data: RAGResponse = await response.json();

        setResults(data.sources);
        setAnswer(showAnswer ? data.answer : null);
        setConfidence(data.confidence);
        setSearchMeta({
          strategy: data.searchStrategy,
          chunks: data.retrievedChunks,
          cached: data.cached,
        });
      } catch (error) {
        console.error('Search error:', error);
        setResults([]);
        setAnswer(null);
      } finally {
        setIsSearching(false);
      }
    },
    [knowledgeBaseId, knowledgeBaseIds, showAnswer],
  );

  const handleInputChange = useCallback(
    (value: string) => {
      setQuery(value);

      // Debounce search
      if (debounceRef.current) {
        clearTimeout(debounceRef.current);
      }

      debounceRef.current = setTimeout(() => {
        handleSearch(value);
      }, 500);
    },
    [handleSearch],
  );

  const handleSubmit = useCallback(
    (e: React.FormEvent) => {
      e.preventDefault();
      if (debounceRef.current) {
        clearTimeout(debounceRef.current);
      }
      handleSearch(query);
    },
    [query, handleSearch],
  );

  const toggleChunkExpand = useCallback((chunkId: string) => {
    setExpandedChunks((prev) => {
      const next = new Set(prev);
      if (next.has(chunkId)) next.delete(chunkId);
      else next.add(chunkId);
      return next;
    });
  }, []);

  useEffect(() => {
    return () => {
      if (debounceRef.current) clearTimeout(debounceRef.current);
    };
  }, []);

  return (
    <Card className={className}>
      <CardHeader>
        <CardTitle className="text-lg flex items-center gap-2">
          <BookOpen className="h-5 w-5" />
          Knowledge Base Search
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        {/* Search Input */}
        <form onSubmit={handleSubmit} className="flex gap-2">
          <div className="relative flex-1">
            <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-muted-foreground" />
            <Input
              value={query}
              onChange={(e) => handleInputChange(e.target.value)}
              placeholder={placeholder}
              className="pl-10"
            />
          </div>
          <Button type="submit" disabled={isSearching || !query.trim()}>
            {isSearching ? (
              <Loader2 className="h-4 w-4 animate-spin" />
            ) : (
              <Search className="h-4 w-4" />
            )}
          </Button>
        </form>

        {/* AI Answer */}
        {answer && (
          <div className="rounded-lg border bg-primary/5 p-4 space-y-3">
            <div className="flex items-center gap-2">
              <Sparkles className="h-4 w-4 text-primary" />
              <span className="text-sm font-medium">AI Answer</span>
              <ConfidenceIndicator confidence={confidence} />
            </div>
            <p className="text-sm leading-relaxed whitespace-pre-wrap">{answer}</p>
            {searchMeta && (
              <div className="flex items-center gap-2 text-xs text-muted-foreground">
                <span>Strategy: {searchMeta.strategy}</span>
                <span>|</span>
                <span>{searchMeta.chunks} sources</span>
                {searchMeta.cached && (
                  <>
                    <span>|</span>
                    <Badge variant="outline" className="text-[10px]">Cached</Badge>
                  </>
                )}
              </div>
            )}
          </div>
        )}

        {/* Results */}
        {results.length > 0 && (
          <div className="space-y-2">
            <h4 className="text-sm font-medium text-muted-foreground">
              Sources ({results.length})
            </h4>
            {results.map((result, index) => {
              const isExpanded = expandedChunks.has(result.id);
              const preview = result.content.substring(0, 200);
              const hasMore = result.content.length > 200;

              return (
                <div
                  key={result.id}
                  className="rounded-lg border p-3 space-y-2 hover:border-primary/30 transition-colors cursor-pointer"
                  onClick={() => onResultSelect?.(result)}
                >
                  <div className="flex items-start justify-between gap-2">
                    <div className="flex items-center gap-2">
                      <Badge variant="outline" className="text-[10px] flex-shrink-0">
                        Source {index + 1}
                      </Badge>
                      <ScoreIndicator score={result.score} />
                    </div>
                    <div className="flex items-center gap-1 text-xs text-muted-foreground">
                      <FileText className="h-3 w-3" />
                      <span>Chunk {result.chunkIndex}</span>
                    </div>
                  </div>

                  <div className="text-sm">
                    <HighlightedText
                      text={isExpanded ? result.content : preview + (hasMore && !isExpanded ? '...' : '')}
                      query={query}
                    />
                  </div>

                  {hasMore && (
                    <Button
                      variant="ghost"
                      size="sm"
                      className="h-6 text-xs"
                      onClick={(e) => {
                        e.stopPropagation();
                        toggleChunkExpand(result.id);
                      }}
                    >
                      {isExpanded ? (
                        <>
                          <ChevronUp className="h-3 w-3 mr-1" />
                          Show less
                        </>
                      ) : (
                        <>
                          <ChevronDown className="h-3 w-3 mr-1" />
                          Show more
                        </>
                      )}
                    </Button>
                  )}

                  {(() => {
                    const meta = result.metadata as Record<string, unknown> | undefined;
                    return meta?.fileName ? (
                      <div className="flex items-center gap-1 text-xs text-muted-foreground">
                        <FileText className="h-3 w-3" />
                        <span>{String(meta.fileName)}</span>
                      </div>
                    ) : null;
                  })()}
                </div>
              );
            })}
          </div>
        )}

        {/* Empty State */}
        {query && !isSearching && results.length === 0 && (
          <div className="text-center py-8 text-muted-foreground">
            <Search className="h-8 w-8 mx-auto mb-2 opacity-50" />
            <p className="text-sm">No results found for &ldquo;{query}&rdquo;</p>
            <p className="text-xs mt-1">Try different keywords or a broader search</p>
          </div>
        )}
      </CardContent>
    </Card>
  );
}

// ---------- Sub-components ----------

function ConfidenceIndicator({ confidence }: { confidence: number }) {
  const percentage = Math.round(confidence * 100);
  const color =
    confidence >= 0.7
      ? 'text-green-600 dark:text-green-400'
      : confidence >= 0.4
        ? 'text-yellow-600 dark:text-yellow-400'
        : 'text-red-600 dark:text-red-400';

  return (
    <span className={cn('text-xs font-medium', color)}>
      {percentage}% confidence
    </span>
  );
}

function ScoreIndicator({ score }: { score: number }) {
  const percentage = Math.round(score * 100);
  const bgColor =
    score >= 0.7
      ? 'bg-green-100 text-green-700 dark:bg-green-900/30 dark:text-green-400'
      : score >= 0.4
        ? 'bg-yellow-100 text-yellow-700 dark:bg-yellow-900/30 dark:text-yellow-400'
        : 'bg-red-100 text-red-700 dark:bg-red-900/30 dark:text-red-400';

  return (
    <span className={cn('text-[10px] font-medium px-1.5 py-0.5 rounded', bgColor)}>
      {percentage}%
    </span>
  );
}

function HighlightedText({ text, query }: { text: string; query: string }) {
  if (!query.trim()) return <span>{text}</span>;

  const terms = query
    .split(/\s+/)
    .filter((t) => t.length > 2)
    .map((t) => t.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'));

  if (terms.length === 0) return <span>{text}</span>;

  const regex = new RegExp(`(${terms.join('|')})`, 'gi');
  const parts = text.split(regex);

  return (
    <span>
      {parts.map((part, i) =>
        regex.test(part) ? (
          <mark key={i} className="bg-yellow-200 dark:bg-yellow-900/50 rounded px-0.5">
            {part}
          </mark>
        ) : (
          <span key={i}>{part}</span>
        ),
      )}
    </span>
  );
}

// ---------- Helpers ----------

function getStoredToken(): string {
  if (typeof window === 'undefined') return '';
  return localStorage.getItem('auth_token') || '';
}

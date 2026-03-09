import { useQuery, useMutation, useQueryClient, type UseQueryOptions } from '@tanstack/react-query';
import api from '@/lib/api';
import type { AxiosError } from 'axios';

interface ApiError {
  message: string;
  statusCode: number;
}

export function useApiQuery<T>(
  key: string[],
  url: string,
  options?: Omit<UseQueryOptions<T, AxiosError<ApiError>>, 'queryKey' | 'queryFn'>
) {
  return useQuery<T, AxiosError<ApiError>>({
    queryKey: key,
    queryFn: async () => {
      const { data } = await api.get<T>(url);
      return data;
    },
    ...options,
  });
}

export function useApiMutation<TData, TVariables>(
  url: string,
  method: 'post' | 'put' | 'patch' | 'delete' = 'post',
  invalidateKeys?: string[][]
) {
  const queryClient = useQueryClient();

  return useMutation<TData, AxiosError<ApiError>, TVariables>({
    mutationFn: async (variables) => {
      const { data } = await api[method]<TData>(url, variables);
      return data;
    },
    onSuccess: () => {
      if (invalidateKeys) {
        invalidateKeys.forEach((key) => {
          queryClient.invalidateQueries({ queryKey: key });
        });
      }
    },
  });
}

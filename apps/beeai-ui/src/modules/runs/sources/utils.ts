/**
 * Copyright 2025 © BeeAI a Series of LF Projects, LLC
 * SPDX-License-Identifier: Apache-2.0
 */

import { v4 as uuid } from 'uuid';

import type { CitationMetadata } from '../api/types';
import type { AgentMessage, ChatMessage } from '../chat/types';
import { isAgentMessage } from '../utils';
import type { ResolvedSource, SourceReference, SourcesData } from './api/types';

export function resolveSource({ source, data }: { source: SourceReference; data: ResolvedSource | undefined }) {
  return data ?? { ...source, metadata: { title: source.url } };
}

export function prepareMessageSources({ message, metadata }: { message: AgentMessage; metadata: CitationMetadata }) {
  const { url, start_index, end_index, title, description } = metadata;
  const { sources: prevSources = [] } = message;

  const key = uuid();

  const sources: SourceReference[] = [
    ...prevSources,
    {
      key,
      url,
      startIndex: start_index,
      endIndex: end_index,
      messageKey: message.key,
      title: title ?? undefined,
      description: description ?? undefined,
    },
  ]
    .sort((a, b) => a.startIndex - b.startIndex)
    .map((source, idx) => ({
      ...source,
      number: idx + 1,
    }));

  const newSource = sources.find((source) => source.key === key)!;

  return { sources, newSource };
}

export function extractSources(messages: ChatMessage[]) {
  const sources = messages.reduce<SourcesData>((data, message) => {
    if (isAgentMessage(message) && message.sources) {
      return {
        ...data,
        [message.key]: message.sources,
      };
    }

    return data;
  }, {});

  return sources;
}

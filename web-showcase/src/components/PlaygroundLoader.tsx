"use client";

import dynamic from "next/dynamic";

/** Reserve the playground's full height so loading it never shifts the page (CLS 0). */
function PlaygroundSkeleton() {
  return (
    <div className="grid min-h-[640px] gap-6 lg:grid-cols-2" aria-busy="true" aria-label="Loading playground">
      <div className="card space-y-4 p-6">
        <div className="skeleton h-6 w-1/3" />
        <div className="grid grid-cols-2 gap-3">
          {[0, 1, 2, 3].map((index) => (
            <div key={index} className="skeleton h-24" />
          ))}
        </div>
        <div className="skeleton aspect-video w-full" />
      </div>
      <div className="card space-y-3 p-6">
        <div className="skeleton h-6 w-1/2" />
        <div className="skeleton h-4 w-full" />
        <div className="skeleton h-4 w-5/6" />
        <div className="skeleton h-40 w-full" />
      </div>
    </div>
  );
}

const DemoPlayground = dynamic(() => import("./DemoPlayground").then((module) => module.DemoPlayground), {
  ssr: false,
  loading: PlaygroundSkeleton,
});

export function PlaygroundLoader() {
  return <DemoPlayground />;
}

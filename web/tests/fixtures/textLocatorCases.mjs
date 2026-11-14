export const textLocatorCases = [
  {
    text: "old quote new quote end",
    quote: "quote",
    surrounding: "new quote end",
    found: [14, 19],
  },
  { text: "old quote new quote end", quote: "quote", surrounding: "changed", found: [4, 9] },
  { text: "a  wrapped\n\tline here", quote: "wrapped line", surrounding: "", found: [3, 16] },
  { text: "one two three", quote: " two\nthree ", surrounding: "", found: [4, 13] },
  { text: "x two y two z", quote: "two", surrounding: "y   two\nz", found: [8, 11] },
  { text: "nothing here", quote: "gone", surrounding: "", found: null },
];

exports.default = async ({page}) => {
  const prototype = Object.getPrototypeOf(page);
  if (typeof prototype.bringToFront !== 'function')
    throw new Error('Unsupported Playwright page');
  prototype.bringToFront = async function () {};
};
